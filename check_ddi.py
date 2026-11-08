"""快速检查：iPhone 的 DDI 挂载状态 + 设备信息

用法：py -3 -X utf8 check_ddi.py

输出里只看最后一段：
  DDI 已挂载  -> 脚本推点会生效，可以跑 ios_loop_gps.py
  DDI 未挂载  -> 这就是「位置不动」的原因，跑 mount_ddi_auto.py 自动挂

退出码：0 = 已挂载，1 = 未挂载或出错。

注意：ImageType 必须用 "Developer"，不是 "DeveloperDiskImage"。
用后者查永远是空，会误判成「未挂载」。
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ios_loop_gps as M   # noqa: E402

ITYPE = "Developer"


def svc(ld, mux, dev, pairing, name):
    resp = ld.start_service(name)
    return M.open_service_port(mux, dev, int(resp["Port"]),
                               bool(resp.get("EnableServiceSSL")), pairing)


def ld_call(ld, obj):
    M.ld_send(ld.sock, obj)
    return M.ld_recv(ld.sock)


def check(itype=ITYPE):
    try:
        mux = M.Usbmux()
    except Exception as e:
        print("连不上 usbmuxd：%s" % e)
        print("（装 iTunes 或 Apple Mobile Device Support，确认服务在跑）")
        return 1

    try:
        dev = M._pick_device(mux, None)
    except Exception as e:
        print("没找到 iPhone：%s" % e)
        print("（检查数据线、手机是否解锁、是否点了「信任」）")
        return 1

    try:
        pairing = M.load_pairing(dev["udid"])
        ld = M.Lockdown(mux, dev, pairing).open()
    except Exception as e:
        print("lockdown 握手失败：%s" % e)
        print("（可能是配对失效，用爱思助手连一次让它重新配对）")
        return 1

    print("设备 UDID: %s" % dev["udid"])
    for k in ("ProductVersion", "ProductType", "DeviceName"):
        try:
            r = ld_call(ld, {"Label": M.LABEL, "Request": "GetValue", "Key": k})
            print("  %-14s %s" % (k, r.get("Value")))
        except Exception as e:
            print("  %-14s <%s>" % (k, e))

    mounted = False
    try:
        s = svc(ld, mux, dev, pairing, "com.apple.mobile.mobile_image_mounter")
        M.ld_send(s, {"Command": "LookupImage", "ImageType": itype})
        r = M.ld_recv(s)
        try:
            s.close()
        except Exception:
            pass
        mounted = bool(isinstance(r, dict) and r.get("ImageSignature"))
        print("\nLookupImage(%s) -> %s" % (itype, r))
    except Exception as e:
        print("\nLookupImage 失败: %s" % e)

    print("\n" + "=" * 56)
    if mounted:
        print("DDI 已挂载 —— 推点会生效，可以跑 ios_loop_gps.py 或 web_gui.py")
    else:
        print("DDI 未挂载 —— 这就是「脚本在跑但位置不动」的原因。")
        print("跑这一句就能挂上：")
        print("  py -3 -X utf8 mount_ddi_auto.py")
    print("=" * 56)

    try:
        M.ld_send(ld.sock, {"Label": M.LABEL, "Request": "StopSession",
                            "SessionID": ld.session_id})
        M.ld_recv(ld.sock)
    except Exception:
        pass
    try:
        ld.close()
    except Exception:
        pass
    return 0 if mounted else 1


def cli():
    ap = argparse.ArgumentParser(
        description="检查 iPhone 的 DDI（DeveloperDiskImage）挂载状态")
    ap.add_argument("--type", default=ITYPE,
                    help='ImageType（默认 Developer，不要改成 DeveloperDiskImage）')
    a = ap.parse_args()
    return check(a.type)


if __name__ == "__main__":
    sys.exit(cli())

