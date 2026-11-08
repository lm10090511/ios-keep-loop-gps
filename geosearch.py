"""地点搜索：输入地名，返回 GCJ-02 经纬度（可直接拿去当地图中心）。

只用标准库。数据源是百度地图的公开搜索接口（`map.baidu.com/?qt=s`），
它返回的是 BD09 墨卡托米制坐标，这里换算成 BD09 经纬度、再转成 GCJ-02，
这样搜到的点和我们用的高德瓦片（GCJ-02）严丝合缝。

不想联网 / 搜不到时，可以直接在界面里拖地图到目标位置，不依赖这个模块。
"""

import json
import math
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# --- BD09 墨卡托 -> BD09 经纬度（百度官方换算表） ------------------------- #
_MC_BAND = [12890594.86, 8362377.87, 5591021.0, 3481989.83, 1678043.12, 0.0]
_MC2LL = [
    [1.410526172116255e-8, 0.00000898305509648872, -1.9939833816331,
     200.9824383106796, -187.2403703815547, 91.6087516669843,
     -23.38765649603339, 2.57121317296198, -0.03801003308653, 17337981.2],
    [-7.435856389565537e-9, 0.000008983055097726239, -0.78625201886289,
     96.32687599759846, -1.85204757529826, -59.36935905485877,
     47.40033549296737, -16.50741931063887, 2.28786674699375, 10260144.86],
    [-3.030883460898826e-8, 0.00000898305509983578, 0.30071316287616,
     59.74293618442277, 7.357984074871, -25.38371002664745,
     13.45380521110908, -3.29883767235584, 0.32710905363475, 6856817.37],
    [-1.981981304930552e-8, 0.000008983055099779535, 0.03278182852591,
     40.31678527705744, 0.65659298677277, -4.44255534477492,
     0.85341911805263, 0.12923347998204, -0.04625736007561, 4482777.06],
    [3.09191371068437e-9, 0.000008983055096812155, 0.00006995724062,
     23.10934304144901, -0.00023663490511, -0.6321817810242,
     -0.00663494467273, 0.03430082397953, -0.00466043876332, 2555164.4],
    [2.890871144776878e-9, 0.000008983055095805407, -3.068298e-8,
     7.47137025468032, -0.00000353937994, -0.02145144861037,
     -0.00001234426596, 0.00010322952773, -0.00000323890364, 826088.5],
]


def _mc_convert(x, y, c):
    t = c[0] + c[1] * abs(x)
    b = abs(y) / c[9]
    u = (c[2] + c[3] * b + c[4] * b ** 2 + c[5] * b ** 3 + c[6] * b ** 4
         + c[7] * b ** 5 + c[8] * b ** 6)
    return (u if y >= 0 else -u), (t if x >= 0 else -t)


def bd09mc_to_bd09ll(x, y):
    """BD09 墨卡托（米）-> BD09 经纬度。"""
    c = _MC2LL[-1]
    for i, band in enumerate(_MC_BAND):
        if abs(y) >= band:
            c = _MC2LL[i]
            break
    return _mc_convert(x, y, c)


def bd09ll_to_gcj(bd_lat, bd_lon):
    """BD09 经纬度 -> GCJ-02（百度官方反算公式）。"""
    x = bd_lon - 0.0065
    y = bd_lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * math.pi * 3000.0 / 180.0)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * math.pi * 3000.0 / 180.0)
    return z * math.sin(theta), z * math.cos(theta)


# --- 搜索 ------------------------------------------------------------------ #
def _fetch(keyword, timeout=12):
    url = ("https://map.baidu.com/?qt=s&wd=" + urllib.parse.quote(keyword)
           + "&c=131&src=0&pn=0&sug=0&l=12&ie=utf-8")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://map.baidu.com/",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw.decode("utf-8", "replace"))


def search(keyword, limit=8, timeout=12):
    """返回 [{"name","addr","lat","lon","tag"}]，lat/lon 是 GCJ-02。

    出错时抛异常，由调用方转成友好提示。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return []

    data = _fetch(keyword, timeout=timeout)
    out = []
    for it in (data.get("content") or []):
        if not isinstance(it, dict):
            continue
        name = (it.get("name") or "").strip()
        if not name:
            continue
        # geo 字段是 "1|x,y;x,y|..."，单位是米（BD09 墨卡托）；
        # x/y 字段是同一坐标 ×100，两条路单位不同，别混用。
        x = y = None
        geo = it.get("geo") or ""
        try:
            seg = geo.split("|")[1].split(";")[0]
            x, y = [float(v) for v in seg.split(",")[:2]]
        except Exception:
            try:
                x = float(it.get("x")) / 100.0
                y = float(it.get("y")) / 100.0
            except Exception:
                continue
        if not x or not y:
            continue
        try:
            bd_lat, bd_lon = bd09mc_to_bd09ll(x, y)
            glat, glon = bd09ll_to_gcj(bd_lat, bd_lon)
        except Exception:
            continue
        if not (-90 < glat < 90 and -180 < glon < 180):
            continue
        out.append({
            "name": name,
            "addr": (it.get("addr") or "").strip(),
            "tag": (it.get("std_tag") or "").replace(";", " · "),
            "lat": round(glat, 7),
            "lon": round(glon, 7),
        })
        if len(out) >= limit:
            break
    return out


if __name__ == "__main__":
    import sys
    kw = sys.argv[1] if len(sys.argv) > 1 else "天安门"
    for i, r in enumerate(search(kw), 1):
        print("%d. %s  (%.6f, %.6f)  %s  %s"
              % (i, r["name"], r["lat"], r["lon"], r["addr"], r["tag"]))
