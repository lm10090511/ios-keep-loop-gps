"""把 GCJ-02 路线（从高德瓦片量出来的）转成 WGS-84（iPhone 要的），
并顺手核对周长、中心、回代误差。

用法：
    py -3 -X utf8 to_wgs.py                      # 转示例路线
    py -3 -X utf8 to_wgs.py 我的路线.json         # 转指定文件
    py -3 -X utf8 to_wgs.py 输入.json 输出.json
"""

import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from coord import gcj2wgs, wgs2gcj   # noqa: E402


def haversine(lat1, lon1, lat2, lon2):
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def perim(pts):
    s = 0.0
    for i in range(len(pts)):
        s += haversine(*pts[i], *pts[(i + 1) % len(pts)])
    return s


def main(src, dst):
    with open(src, encoding="utf-8") as f:
        doc = json.load(f)
    if isinstance(doc, list):
        raw = doc
        note = ""
    else:
        raw = doc.get("points") or doc.get("route") or []
        note = doc.get("_说明", "")

    pts = []
    for p in raw:
        if isinstance(p, dict):
            pts.append((float(p["lat"]), float(p["lon"])))
        else:
            pts.append((float(p[0]), float(p[1])))
    if len(pts) < 2:
        print("路线里点数太少（%d），至少要 2 个点。" % len(pts))
        return 1

    wgs = [gcj2wgs(la, lo) for la, lo in pts]

    c_la = sum(p[0] for p in pts) / len(pts)
    c_lo = sum(p[1] for p in pts) / len(pts)
    w_la = sum(p[0] for p in wgs) / len(wgs)
    w_lo = sum(p[1] for p in wgs) / len(wgs)

    print("源（GCJ-02）：%d 点，周长 %.1f m，中心 %.7f,%.7f"
          % (len(pts), perim(pts), c_la, c_lo))
    print("转（WGS-84）：%d 点，周长 %.1f m，中心 %.7f,%.7f"
          % (len(wgs), perim(wgs), w_la, w_lo))
    print("中心位移 %.1f m（这个值正常在 400–600 米之间）"
          % haversine(c_la, c_lo, w_la, w_lo))

    worst = 0.0
    for (la, lo), (wa, wo) in zip(pts, wgs):
        bl, bo = wgs2gcj(wa, wo)
        worst = max(worst, abs(bl - la), abs(bo - lo))
    print("回代最大误差 %.2e 度（应该 < 1e-9）" % worst)

    out = {
        "_说明": (note.replace("GCJ-02", "").strip()
                  + "（坐标已转 WGS-84，可直接给 iPhone 用）").strip(),
        "_坐标系": "WGS-84",
        "_怎么改成你想要的路线": "改 points 里的经纬度即可；若从高德/腾讯地图取点，记得先转成 WGS-84",
        "points": [{"lat": round(la, 7), "lon": round(lo, 7)} for la, lo in wgs],
    }
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("saved %s" % dst)
    return 0


def cli():
    ap = argparse.ArgumentParser(
        description="把 GCJ-02 路线转成 WGS-84（iPhone 的 simulatelocation 要 WGS-84）")
    ap.add_argument("src", nargs="?", default=None,
                    help="输入路线（默认 example-tiananmen.json）")
    ap.add_argument("dst", nargs="?", default="route-out-wgs.json",
                    help="输出文件（默认 route-out-wgs.json）")
    a = ap.parse_args()

    src = a.src
    if src is None:
        src = os.path.join(HERE, "routes", "example-tiananmen.json")
        if not os.path.exists(src):
            print("没找到默认路线 %s，请指定输入文件。" % src)
            return 1
    if not os.path.exists(src):
        print("找不到文件：%s" % src)
        return 1

    try:
        return main(src, a.dst)
    except Exception as e:
        print("转换失败：%s: %s" % (type(e).__name__, e))
        return 1


if __name__ == "__main__":
    sys.exit(cli())
