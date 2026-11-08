"""GCJ-02（高德/腾讯用的火星坐标）→ WGS-84（GPS/iPhone 用的真实坐标）。

为什么需要：我们从高德卫星瓦片量出来的经纬度是 GCJ-02，
而 iPhone 的 com.apple.dt.simulatelocation 服务要的是 WGS-84。
国内两者差几百米（华北一带约 400–600 m），直接推就会偏出目标位置。
"""

import math

PI = 3.1415926535897932384626
A = 6378245.0
EE = 0.00669342162296594323


def _tlat(x, y):
    r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    r += (20.0 * math.sin(y * PI) + 40.0 * math.sin(y / 3.0 * PI)) * 2.0 / 3.0
    r += (160.0 * math.sin(y / 12.0 * PI) + 320.0 * math.sin(y * PI / 30.0)) * 2.0 / 3.0
    return r


def _tlon(x, y):
    r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    r += (20.0 * math.sin(x * PI) + 40.0 * math.sin(x / 3.0 * PI)) * 2.0 / 3.0
    r += (150.0 * math.sin(x / 12.0 * PI) + 300.0 * math.sin(x / 30.0 * PI)) * 2.0 / 3.0
    return r


def out_of_china(lat, lon):
    return not (73.66 < lon < 135.05 and 3.86 < lat < 53.55)


def wgs2gcj(lat, lon):
    if out_of_china(lat, lon):
        return lat, lon
    dlat = _tlat(lon - 105.0, lat - 35.0)
    dlon = _tlon(lon - 105.0, lat - 35.0)
    rad = lat / 180.0 * PI
    magic = 1 - EE * math.sin(rad) ** 2
    sq = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((A * (1 - EE)) / (magic * sq) * PI)
    dlon = (dlon * 180.0) / (A / sq * math.cos(rad) * PI)
    return lat + dlat, lon + dlon


def gcj2wgs(lat, lon):
    """迭代反解，精度 < 1e-9 度（约 0.1 mm）。"""
    if out_of_china(lat, lon):
        return lat, lon
    wlat, wlon = lat, lon
    for _ in range(30):
        glat, glon = wgs2gcj(wlat, wlon)
        dlat = glat - lat
        dlon = glon - lon
        wlat -= dlat
        wlon -= dlon
        if abs(dlat) < 1e-9 and abs(dlon) < 1e-9:
            break
    return wlat, wlon


def offset_m(lat, lon):
    """GCJ-02 相对 WGS-84 的偏移量（米），用于自检。"""
    wlat, wlon = gcj2wgs(lat, lon)
    dy = (lat - wlat) * 111320.0
    dx = (lon - wlon) * 111320.0 * math.cos(math.radians(lat))
    return math.hypot(dx, dy), dx, dy


if __name__ == "__main__":
    tests = [
        ("北京天安门", 39.908722, 116.397499),
        ("上海人民广场", 31.230416, 121.473701),
        ("广州天河体育中心", 23.139390, 113.324470),
    ]
    for name, la, lo in tests:
        wla, wlo = gcj2wgs(la, lo)
        d, dx, dy = offset_m(la, lo)
        back = wgs2gcj(wla, wlo)
        print("%s  GCJ(%.7f,%.7f) -> WGS(%.7f,%.7f)  偏移 %.1f m (dx=%.1f dy=%.1f)  回代误差 %.2e,%.2e"
              % (name, la, lo, wla, wlo, d, dx, dy, back[0] - la, back[1] - lo))
