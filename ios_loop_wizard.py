#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""iOS 循环虚拟定位 —— 交互式向导。

不知道从哪开始就跑这个：它按顺序做体检 → 检查/挂载 DDI → 选路线 → 选配速 → 开始推点。

用法：
    py -3 -X utf8 ios_loop_wizard.py

（Windows 上直接双击「循环定位-网页版.bat」更省事，那个是网页版。）
"""

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "ios_loop_gps.py")
CHECK_DDI = os.path.join(HERE, "check_ddi.py")
MOUNT_DDI = os.path.join(HERE, "mount_ddi_auto.py")
ROUTES_DIR = os.path.join(HERE, "routes")


def pause():
    try:
        input("\n按回车键退出...")
    except Exception:
        pass


def ddi_ready():
    """跑 check_ddi.py，返回 True 表示 DDI 已挂载。"""
    try:
        rc = subprocess.call([sys.executable, "-X", "utf8", CHECK_DDI])
    except Exception as exc:
        print("  检查失败：%s" % exc)
        return False
    return rc == 0


def main():
    print("=" * 62)
    print(" iOS 循环虚拟定位")
    print("=" * 62)

    print("\n[1/5] 检查环境和设备 ...")
    rc = subprocess.call([sys.executable, "-X", "utf8", SCRIPT, "--probe"])
    if rc != 0:
        print("\n体检没通过，按上面提示处理后重新运行。")
        pause()
        return rc

    print("\n[2/5] 检查 DDI 挂载状态 ...")
    print("      没有 DDI 时，点包发进去是黑洞 —— 脚本会跑、但手机地图不动。\n")
    if not ddi_ready():
        print("\nDDI 没挂上，尝试自动挂载 ...\n")
        subprocess.call([sys.executable, "-X", "utf8", MOUNT_DDI])
        if not ddi_ready():
            print("\n还是没挂上。看 mount_ddi_auto.py 输出的提示。")
            pause()
            return 1

    print("\n[3/5] 选择路线")
    found = []
    if os.path.isdir(ROUTES_DIR):
        found = sorted(f for f in os.listdir(ROUTES_DIR) if f.endswith(".json"))
    if not found:
        print("  当前目录里没有路线文件。")
        pause()
        return 1
    for i, f in enumerate(found, 1):
        print("  %d) %s" % (i, f[:-5]))
    pick = input("请输入序号 [默认 1]: ").strip() or "1"
    try:
        idx = int(pick) - 1
        if idx < 0 or idx >= len(found):
            idx = 0
    except ValueError:
        idx = 0
    route = os.path.join(ROUTES_DIR, found[idx])
    print("     注意：从高德/腾讯地图取的坐标是 GCJ-02，路线文件里要写")
    print('           "_坐标系": "GCJ-02"，程序会自动转成 WGS-84；')
    print("           不写则按 WGS-84 处理（会偏 400–600 米）。")

    print("\n[4/5] 选择配速")
    print("  1) 6'00\"/km（慢跑）")
    print("  2) 5'00\"/km")
    print("  3) 4'00\"/km")
    print("  4) 3'00\"/km（快，出样本最快）")
    choice = input("请输入序号 [默认 1]: ").strip() or "1"
    speed = {"1": 1000 / 360, "2": 1000 / 300,
             "3": 1000 / 240, "4": 1000 / 180}.get(choice, 1000 / 360)

    print("\n[5/5] 开始循环推送（Ctrl+C 停止）")
    print("     路线文件：%s" % route)
    print("     想换路线就编辑 json 里的 points（至少 3 个 [纬度,经度]）。\n")
    try:
        rc = subprocess.call([sys.executable, "-X", "utf8", SCRIPT,
                              "--route", route,
                              "--speed", "%.3f" % speed,
                              "--hz", "1",
                              "--loops", "0"])
    except KeyboardInterrupt:
        rc = 0
    print("\n已停止。iOS 过几分钟会自动回到真实定位；要立刻恢复请运行：")
    print("  py -3 -X utf8 recover.py")
    pause()
    return rc


def cli():
    ap = argparse.ArgumentParser(
        description="iOS 循环虚拟定位向导：体检 → 挂 DDI → 选路线 → 选配速 → 推点")
    ap.add_argument("--version", action="version", version="ios-keep-loop-gps 1.0.0")
    ap.parse_args()
    try:
        return main()
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130


if __name__ == "__main__":
    sys.exit(cli())
