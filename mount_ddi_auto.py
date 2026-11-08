"""自动挂载 iPhone 的 DeveloperDiskImage（DDI），不需要爱思助手。

关键点（都是实测踩出来的）：
  1. ImageType 必须用 "Developer"，不是 "DeveloperDiskImage"。
     用后者会报 `Unsupported disk image type: DeveloperDiskImage`。
  2. ReceiveBytes 收完数据后，文件落在设备的
       /private/var/run/mobile_image_mounter/<SIG_HASH>/<SIG2>/<random>.dmg
     此时 MountImage 会去找它；ImagePath 传什么不影响（设备用自己算出来的路径）。
  3. MountImage 的 ImageSignature 传空 data 也行，但带上 sig 更稳。
  4. 挂载是否成功，用 LookupImage(ImageType="Developer") 看 ImageSignature 是否非空。

用法：
  py -3 -X utf8 mount_ddi_auto.py                  # 自动找镜像
  py -3 -X utf8 mount_ddi_auto.py --dmg X --sig Y  # 指定镜像
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ios_loop_gps as M

HERE = os.path.dirname(os.path.abspath(__file__))
IMG_PATH = "/private/var/mobile/Media/PublicStaging/staging.dimage"

# 镜像不随仓库分发（Apple 的 DDI 有版权，体积也大）。按下面的顺序自动找：
#   1. 仓库里的 ddi/ 目录（你自己放进去）
#   2. 爱思助手缓存目录（装了爱思的话通常已经有现成的）
#   3. 命令行 --dmg/--sig 指定
DDI_DIR = os.path.join(HERE, "ddi")
# 爱思助手的默认安装位置（装的时候可以改，所以多列几个常见路径）
I4TOOLS_HINTS = [
    os.path.join("C:\\Program Files", "i4Tools9", "cache", "other"),
    os.path.join("C:\\Program Files (x86)", "i4Tools9", "cache", "other"),
    os.path.join(os.path.expanduser("~"), "i4Tools9", "cache", "other"),
]
# 各盘符下常见的安装布局（爱思装到 D 盘之类的情况）
I4TOOLS_LAYOUTS = [
    os.path.join("i4Tools9", "cache", "other"),
    os.path.join("tool", "i4Tools9", "cache", "other"),
    os.path.join("Program Files", "i4Tools9", "cache", "other"),
    os.path.join("Program Files (x86)", "i4Tools9", "cache", "other"),
]


def _drive_roots():
    """Windows 上枚举存在的盘符；其它系统返回空。"""
    if os.name != "nt":
        return []
    roots = []
    for letter in "CDEFGH":
        root = letter + ":\\"
        if os.path.isdir(root):
            roots.append(root)
    return roots
DDI_NAME_HINTS = ["ss_ios155", "ss_ios154", "ss_ios152", "ss_ios150"]


def find_ddi():
    """返回 (dmg路径, sig路径)，找不到返回 (None, None)。

    查找顺序：
      1. 环境变量 DDI_DIR 指向的目录
      2. 仓库自带 ddi/ 目录
      3. 爱思助手的常见缓存目录
    """
    dirs = []
    env = os.environ.get("DDI_DIR")
    if env:
        dirs.append(env)
    dirs.append(DDI_DIR)
    for d in dirs:
        if not os.path.isdir(d):
            continue
        dmgs = [f for f in sorted(os.listdir(d)) if f.lower().endswith(".dmg")]
        for name in dmgs:
            dmg = os.path.join(d, name)
            sig = dmg[:-4] + ".sig"
            if os.path.exists(sig):
                return dmg, sig
            alt = os.path.join(d, os.path.splitext(name)[0] + ".sig")
            if os.path.exists(alt):
                return dmg, alt
    # 爱思缓存目录（常见固定路径 + 各盘符常见布局）
    cands = list(I4TOOLS_HINTS)
    for root in _drive_roots():
        for layout in I4TOOLS_LAYOUTS:
            cands.append(os.path.join(root, layout))
    for base in cands:
        if not os.path.isdir(base):
            continue
        for name in DDI_NAME_HINTS:
            dmg = os.path.join(base, name + ".dmg")
            sig = os.path.join(base, name + ".sig")
            if os.path.exists(dmg) and os.path.exists(sig):
                return dmg, sig
    return None, None


def rd(p):
    with open(p, 'rb') as f:
        return f.read()


def new_conn(ld, mux, dev, pairing):
    r = ld.start_service("com.apple.mobile.mobile_image_mounter")
    return M.open_service_port(mux, dev, int(r["Port"]),
                               bool(r.get("EnableServiceSSL")), pairing)


def lookup(ld, mux, dev, pairing, itype="Developer"):
    s = new_conn(ld, mux, dev, pairing)
    try:
        M.ld_send(s, {"Command": "LookupImage", "ImageType": itype})
        return M.ld_recv(s)
    finally:
        try:
            s.close()
        except Exception:
            pass


def mount(ld, mux, dev, pairing, dmg, sig, itype="Developer"):
    s = new_conn(ld, mux, dev, pairing)
    try:
        M.ld_send(s, {"Command": "ReceiveBytes", "ImageSize": len(dmg),
                      "ImageType": itype, "ImageSignature": sig})
        ack = M.ld_recv(s)
        if ack.get("Status") != "ReceiveBytesAck":
            return {"stage": "ReceiveBytes", "resp": ack}
        s.sendall(dmg)
        done = M.ld_recv(s)
        if done.get("Status") != "Complete":
            return {"stage": "upload", "resp": done}
        M.ld_send(s, {"Command": "MountImage", "ImagePath": IMG_PATH,
                      "ImageType": itype, "ImageSignature": sig})
        return {"stage": "MountImage", "resp": M.ld_recv(s)}
    finally:
        try:
            s.close()
        except Exception:
            pass


def main(argv=None):
    """挂载 DDI。

    argv=None 时从命令行读参数；被别的程序 import 调用时传 argv=[]，
    否则会去解析宿主程序的 sys.argv（比如 web_gui.py 的 --port）而报错退出。
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--dmg", default=None, help="DDI 镜像路径，默认自动找")
    ap.add_argument("--sig", default=None, help="签名文件路径，默认与 dmg 同名 .sig")
    ap.add_argument("--type", default="Developer")
    args = ap.parse_args(argv)

    dmg_path, sig_path = args.dmg, args.sig
    if not dmg_path:
        dmg_path, sig_path = find_ddi()
    if not dmg_path:
        print("找不到 DeveloperDiskImage。三种办法任选：")
        print("  1) 装了爱思助手：打开它连一次手机，让它自己下好 DDI，然后重跑本脚本")
        print("     （脚本会自动去爱思的 cache/other 目录里找 ss_ios15x.dmg）")
        print("  2) 把 dmg 和同名 .sig 放到本目录的 ddi/ 下")
        print("  3) 用 --dmg 路径 --sig 路径 直接指定")
        print()
        print("注意：DDI 需要与手机 iOS 版本匹配。iPhone 15.5–15.9 用 ss_ios155。")
        return 2
    if not os.path.exists(dmg_path):
        print("找不到镜像：%s" % dmg_path)
        return 2
    if not sig_path:
        cand = os.path.splitext(dmg_path)[0] + ".sig"
        sig_path = cand if os.path.exists(cand) else None

    dmg = rd(dmg_path)
    sig = rd(sig_path) if (sig_path and os.path.exists(sig_path)) else b""
    print("镜像: %s (%d 字节)" % (dmg_path, len(dmg)))

    mux = M.Usbmux()
    dev = M._pick_device(mux, None)
    pairing = M.load_pairing(dev["udid"])
    ld = M.Lockdown(mux, dev, pairing).open()
    print("设备: %s" % dev["udid"])
    for k in ("ProductVersion", "ProductType"):
        try:
            M.ld_send(ld.sock, {"Label": M.LABEL, "Request": "GetValue", "Key": k})
            print("  %-14s %s" % (k, M.ld_recv(ld.sock).get("Value")))
        except Exception:
            pass

    before = lookup(ld, mux, dev, pairing, args.type)
    if before.get("ImageSignature"):
        print("\nDDI 已经是挂载状态，无需操作。")
    else:
        print("\n开始挂载 %s (%d 字节)…" % (os.path.basename(dmg_path), len(dmg)))
        r = mount(ld, mux, dev, pairing, dmg, sig, args.type)
        resp = r.get("resp", {})
        err = resp.get("Error")
        if err:
            msgs = re.findall(r'NSLocalizedDescription=([^}]+)', resp.get("DetailedError", ""))
            print("  挂载失败: %s | %s" % (err, (msgs[-1] if msgs else "")[:200]))
        else:
            print("  挂载响应: %s" % resp)

    after = lookup(ld, mux, dev, pairing, args.type)
    ok = bool(after.get("ImageSignature"))
    print("\n" + "=" * 52)
    print("DDI 已挂载 ✓  可以跑 ios_loop_gps.py 了" if ok
          else "DDI 仍未挂载 ✗  看上面的错误")
    print("=" * 52)

    try:
        M.ld_send(ld.sock, {"Label": M.LABEL, "Request": "StopSession",
                            "SessionID": ld.session_id})
        M.ld_recv(ld.sock)
    except Exception:
        pass
    ld.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
