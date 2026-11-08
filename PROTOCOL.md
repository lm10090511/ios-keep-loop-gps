# 协议备忘

这份文档记录逆向 `idm_other.dll`（爱思助手 9.21 的 iOS 模块）+ 实机抓包得出的
iOS 模拟定位链路。想自己用别的语言实现，看这一份就够。

---

## 整体链路

```
你的脚本
  │  ① usbmux 协议（TCP 127.0.0.1:27015）
  ▼
usbmuxd（iTunes / Apple Mobile Device Support 提供）
  │  ② 建立到设备 62078 端口的隧道
  ▼
lockdownd（设备上的守护进程）
  │  ③ StartSession（可能升级 TLS）
  │  ④ StartService("com.apple.dt.simulatelocation")
  ▼
simulatelocation 服务（设备上另一个端口）
  │  ⑤ 发坐标报文
  ▼
locationd → DTLocationsimulation.dylib → 系统定位
```

---

## ① usbmux：16 字节帧头

小端，4 个 u32：

```
[length][version=1][message_type][tag]
```

- `length` 含 16 字节头
- `message_type = 8` 是 plist 请求（本项目只用这个）
- `tag` 随便给，回包会带回同一个 tag

请求体是 XML plist。列设备：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>MessageType</key><string>ListDevices</string>
  <key>ClientVersionString</key><string>loopgps</string>
  <key>ProgName</key><string>loopgps</string>
  <key>kLibUSBMuxVersion</key><integer>3</integer>
</dict></plist>
```

**连端口时 `PortNumber` 必须发 `htons(port)`**，即 `((p & 0xFF) << 8) | ((p >> 8) & 0xFF)`。
直接发 62078 会被回 `{"MessageType":"Result","Number":3}`（拒绝），
发 32498（= htons(62078)）才成功。这个坑很难从错误码看出来。

---

## ② 隧道内：4 字节大端长度 + plist

**这是最容易卡住的地方。** usbmux 用 16 字节帧头，但隧道建立之后，
lockdown 报文换成 **4 字节大端长度前缀 + XML plist**：

```
[length:u32 BE][XML plist]
```

用错格式的表现：隧道能建、socket 不发数据时一直活着，
但**一发第一个字节设备就 EOF**（`recv` 返回 0），换什么 payload 都一样。
第一次遇到会以为是配对失效或设备没解锁。

---

## ③ StartSession

```xml
<dict>
  <key>Label</key><string>loopgps</string>
  <key>Request</key><string>StartSession</string>
  <key>HostID</key><string>（配对记录里的 HostID）</string>
  <key>SystemBUID</key><string>（配对记录里的 SystemBUID）</string>
</dict>
```

回包：

```xml
<dict>
  <key>Request</key><string>StartSession</string>
  <key>EnableSessionSSL</key><true/>
  <key>SessionID</key><string>XXXXXXXX-....</string>
</dict>
```

`EnableSessionSSL` 为真时**必须立刻升级 TLS**，客户端证书用配对记录里的
`HostCertificate` + `HostPrivateKey`。TLS 之后仍然用「4 字节大端长度 + plist」。

配对记录在 `C:\ProgramData\Apple\Lockdown\<UDID>.plist`（Windows）。

---

## ④ StartService

```xml
<dict>
  <key>Label</key><string>loopgps</string>
  <key>Request</key><string>StartService</string>
  <key>Service</key><string>com.apple.dt.simulatelocation</string>
</dict>
```

回包给出 `Port` 和 `EnableServiceSSL`，再用 usbmux 连那个端口（同样要 `htons`），
必要时再升级一次 TLS。

> **服务永远注册着。** 即使 DDI 没挂载，`StartService` 也会成功、
> 点包也发得出去、**不报任何错**。所以「能发点」不等于「定位生效」。

---

## ⑤ 推点报文

```
[8 字节大端 len(lat)][lat 字符串][4 字节大端 len(lon)][lon 字符串]
```

- lat/lon 用 `"%.9f"` 格式化（12–13 个字符）
- 总长 = `len(lat) + len(lon) + 12`

例：`39.908722000`（13 字符）+ `116.397499000`（13 字符）→ 38 字节

```
000000000000000d 33392e393038373232303030 0000000d 3131362e333937343939303030
└─ 13 (BE u64) ─┘ └──── lat ────────┘ └13 (BE)─┘ └──── lon ────────┘
```

反编译里这段长这样，容易看错：

```c
local_88 = CONCAT17((char)len1, (undefined7)local_88);   // 长度写进 8 字节头
memcpy(&uStack_80, lat_str, len1);                        // lat 在偏移 8
*(char *)((longlong)&uStack_80 + len1 + 3) = (char)len2;  // lon 长度
memcpy((void *)((longlong)&uStack_80 + len1 + 4), lon_str, len2);
```

`CONCAT17` 在小端机器上把 `len1` 放到**偏移 7**（8 字节结构的高字节），
等价于「8 字节大端整数」。如果按「第 0 字节是长度」写，设备会把它当成
天文数字长度的包**直接丢弃** —— 表现就是脚本在跑、地图不动。

---

## ⑥ 恢复真实定位

向同一服务发 4 字节：

```
00 00 00 01
```

对应反编译里的 `local_res8[0] = 0x1000000`（小端存下来就是 `00 00 00 01`）。
写反成 `01 00 00 00` 会让定位卡住不恢复。

---

## ⑦ 连接寿命

- **服务连接**：空闲 1–3 秒就被设备踢掉，必须「连上就发、断了重连」。
- **lockdown 会话**：可以长期复用，不要每点重建（1Hz 跑十几分钟会堆上千会话，
  把 `locationd` 拖死）。
- **不要发 `StopService`**：会让整个 lockdown 会话作废，后续 `StartService` 直接 EOF。
  只 `close()` 服务 socket 就行。
- 退出时发一次 `StopSession` 收尾，避免会话泄漏。

---

## ⑧ DDI（DeveloperDiskImage）

坐标真正被消费的是 `locationd` 里的 `DTLocationsimulation.dylib`，
**只有 DDI 挂载后才会加载**。

```
StartService("com.apple.mobile.mobile_image_mounter")
  → {"Command":"LookupImage","ImageType":"Developer"}
  → ImageSignature 非空 = 已挂载
```

挂载：

```
ReceiveBytes {ImageSize, ImageType:"Developer", ImageSignature}
  → 发送 dmg 字节
  → MountImage {Command:"MountImage", ImagePath:"/private/var/mobile/Media/PublicStaging/staging.dimage", ImageType:"Developer"}
```

**`ImageType` 必须是 `"Developer"`**，不是 `"DeveloperDiskImage"`。
写错会得到 `Unsupported disk image type: DeveloperDiskImage`；
不给 ImageType 得到 `MissingImageType`；给了但文件不在得到 `MissingImagePath`。

DDI 镜像需要与 iOS 版本匹配，且 15.x 之后需要 Apple TSS 个性化签名
（本项目的做法：复用爱思助手已经下好的镜像，它内部有自己的签名通道）。
