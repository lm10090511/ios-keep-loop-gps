"""恢复 iPhone 的真实定位。

什么时候需要它：
  - 脚本异常退出、没走到收尾流程
  - 手机地图一直停在虚拟点不动
  - 想立刻回到真实位置（不等系统自己校准）

原理：向 com.apple.dt.simulatelocation 服务发 4 字节 00 00 00 01。
这个字节序来自 idm_other.dll 的 ios_reset_simulatelocation：
    local_res8[0] = 0x1000000;          // 小端存下来就是 00 00 00 01
    FUN_180006140(*param_1, local_res8, 4, ...)
写反成 01 00 00 00 会让定位卡住不恢复。

用法：
    py -3 -X utf8 recover.py
    py -3 -X utf8 recover.py --times 5
"""

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ios_loop_gps as M   # noqa: E402

RESET = b"\x00\x00\x00\x01"


def do_recover(times=3, delay=1.0):
    try:
        mux = M.Usbmux()
    except Exception as e:
        print("连不上 usbmuxd：%s" % e)
        print("→ 确认装了 iTunes 或 Apple Mobile Device Support，且服务在跑。")
        return 1

    try:
        dev = M._pick_device(mux, None)
    except Exception as e:
        print("没找到 iPhone：%s" % e)
        print("→ 插上手机、解锁、点『信任此电脑』。")
        return 1

    print("设备: %s" % dev["udid"])
    try:
        pairing = M.load_pairing(dev["udid"])
        ld = M.Lockdown(mux, dev, pairing).open()
    except Exception as e:
        print("lockdown 握手失败：%s" % e)
        print("→ 可能是配对失效，用爱思助手连一次让它重新配对。")
        return 1

    print("lockdown ok, SessionID = %s" % ld.session_id)

    sent = 0
    for i in range(1, times + 1):
        try:
            resp = ld.start_service(M.SERVICE)
            port = int(resp["Port"])
            sock = M.open_service_port(mux, dev, port,
                                       bool(resp.get("EnableServiceSSL")), pairing)
            sock.sendall(RESET)
            sent += 1
            print("  第 %d/%d 次复位已发送（port=%d）" % (i, times, port))
            time.sleep(delay)
            try:
                sock.close()
            except Exception:
                pass
            time.sleep(0.4)
        except Exception as e:
            print("  第 %d 次失败：%s: %s" % (i, type(e).__name__, e))

    # 正确收尾，避免会话泄漏在设备上
    try:
        M.ld_send(ld.sock, {"Label": M.LABEL, "Request": "StopSession",
                            "SessionID": ld.session_id})
        M.ld_recv(ld.sock)
        print("StopSession ok")
    except Exception as e:
        print("StopSession 失败（不影响定位）：%s" % e)
    try:
        ld.close()
    except Exception:
        pass

    print()
    if sent:
        print("完成，共发送 %d 次复位。看手机地图是否回到真实位置。" % sent)
        print("如果还是不动：拔线 → 设置 → 通用 → 传输或还原 → 还原位置与隐私 → 重启手机。")
        return 0
    print("一次都没发出去。看上面的报错。")
    return 1


def cli():
    ap = argparse.ArgumentParser(description="把 iPhone 定位恢复成真实位置")
    ap.add_argument("--times", type=int, default=3, help="发几次复位包（默认 3）")
    ap.add_argument("--delay", type=float, default=1.0, help="每次间隔秒数（默认 1.0）")
    a = ap.parse_args()
    return do_recover(max(1, a.times), max(0.0, a.delay))


if __name__ == "__main__":
    sys.exit(cli())
