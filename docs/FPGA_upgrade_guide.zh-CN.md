# QHY600M Pro ERIS FPGA 升级说明（sub2 = 0x12）

> **已做闭环测试：** 固件升级（本 Release 的文件和工具）和采集测试 demo 已在两台 QHY600M Pro ERIS 相机上通过 USB 做过闭环测试，详见第 8 节。

本文说明 GitHub Release `qhy600-fpga-0x12-20261012` 中发布的 FPGA 用户镜像和升级工具的使用方法。

## 1. 发布内容

| 附件 | 内容 |
|---|---|
| `QHY600_FPGA_26-09-24_sub2-0x12.zip` | FPGA 用户镜像：`remoteupdate.jic`、`remoteupdate_user_rpd_auto.rpd`、`remoteupdate_user_rpd.map`、`SHA256SUMS.txt`、`README.txt` |
| `QHY_UPDATE_TOOL_win-x64.zip` | 升级工具 QHYCCD Upgrade Tool V260518（Windows，x64）：`QHY_UPDATE_TOOL.exe` 和它需要的库文件 |
| `SHA256SUMS.txt` | 两个 zip 的校验值 |

镜像版本（从运行该镜像的相机上读出）：

| 项 | 值 |
|---|---|
| FPGA 日期 | 26-09-24 |
| subversion1 | 0x80（user 镜像） |
| subversion2 | 0x12 |
| 板型 | 0x02（ERIS 板，QHY600M Pro ERIS） |

由工程 `ERIS_455_NIOS_1` 编译，输出包为 `remoteupgrade_20261012_09_40_49`。

## 2. 关于这个版本

以下内容来自多机同步调试记录，不是完整的改动清单：

- 镜像支持 USB 和 PCIe/光纤两种连接下的主从同步拍摄（主机向从机单向送触发）。
- 0x0F 版本在 PCIe 发送路径（`PCIE/bypass_to_xgmii.v`）增加了写使能门控和帧尾冲刷。0x10 到 0x12 是诊断版本，尝试把帧计数图案盖写进图像数据流。测试中这个盖写没有出现在 SDK 交付的缓冲区里，所以帧号改用 GPS 帧头里的 `seq_number` 读取。
- 已知限制：PCIe 下使用有限窗口 burst 时，窗口开头可能有少数几帧没有送达（例如补包 8192 时 30 帧只收到 28 帧），原因没有完全查清。同步测试的数据见 `docs/` 里的 PCIe 报告。

## 3. 开始之前

- 确认板型：本升级针对 ERIS 板（板型 0x02），不要用于其他型号的相机。
- **远程升级只能走 USB。** 通过 PCIe/光纤连接的相机无法用该工具升级（该路径没有写 flash 的命令），请用 USB 线连接相机。
- 关闭其他正在使用相机的程序。
- 升级过程中不要断电或拔线。每台相机远程升级约需 20 分钟。
- 开始前用 `SHA256SUMS.txt` 核对文件。

## 4. 方法 A：用工具远程升级（USB）

1. 把 `QHY_UPDATE_TOOL_win-x64.zip` 解压到一个文件夹，`qhyccd.dll`、`tbb.dll`、`winusb.dll` 要和 `QHY_UPDATE_TOOL.exe` 放在同一个文件夹。需要已安装 QHY 相机的 USB 驱动，以及 .NET Framework 4.8。
2. 解压 `QHY600_FPGA_26-09-24_sub2-0x12.zip`。
3. 用 USB 连接一台相机，启动 `QHY_UPDATE_TOOL.exe`。
4. 在侧边栏打开 **主控FPGA升级** 页面。
5. 点 **扫描**，等出现 “SCAN DONE” 提示，在设备列表里选中相机（随后会弹出 “connect success”）。点 **Read Version**，记下升级前的版本。日志里的 `Transmission board version : 128-18-54` 表示 subversion1 = 128（0x80，user 镜像）、subversion2 = 18（0x12）；第三个数字本说明不使用。
6. 点 **UPGRADE USER**。
7. 工具会依次要求选两个文件：
   - 先选 map 文件：`remoteupdate_user_rpd.map`（用来取得用户镜像的结束地址）；
   - 再选镜像文件：`remoteupdate_user_rpd_auto.rpd`。
8. 等待擦除和写入完成，进度显示在日志窗口和进度条里；擦除 85 个扇区约 1 分钟，写入约 22 分钟。升级成功时日志末尾会出现类似下面的行：
   ```
   Upgrade Done
   Upgrade time: 00:23:10
   Now we try to run the new firmware
   Transmission board data : 26-9-24
   The camera was boot on the user firmware image mode
   ```
   写完后工具会再读一次版本，最后一个数字可能显示 0（例如 `128-18-0`），给相机断电上电之后才会稳定，这是正常现象。
9. **给相机断电再上电。**
10. 重新启动工具，扫描后点 **Read Version**。刚上电时寄存器可能返回 0 或旧值，几秒后再读一次。期望结果是第 1 节里的版本。
11. 另一台相机重复以上步骤，两台依次升级。

## 5. 方法 B：用 JIC 文件 JTAG 烧录

适用于有 USB-Blaster 和 Quartus Prime Pro 18.1（Quartus Programmer）的情况。

1. 把 USB-Blaster 接到相机的 JTAG 接口，并给相机供电。
2. 烧录 **JIC 文件**（不要用 SOF 文件）。本工程是双镜像设计，重新加载后 FPGA 会从 flash 加载镜像，把用 SOF 下载进去的内容覆盖掉。
   ```
   quartus_pgm -c "USB-Blaster [USB-0]" -m jtag -o "pvbi;remoteupdate.jic"
   ```
3. 烧录约需 3 分钟。不要同时用两个 USB-Blaster 编程，两台相机依次烧。
4. **给相机断电再上电。** 上电之后才会加载新镜像。
5. 按方法 A 第 10 步读取版本。
6. 判断是否真的烧录了，要看编程器的输出，不能只看“successful”：真正烧录会有配置步骤的输出并持续数分钟，几秒就结束的运行没有烧录设备。

## 6. 核对结果

- FPGA 日期 26-09-24，subversion1 为 0x80，subversion2 为 0x12，板型 0x02。
- 如果两次断电上电后相机仍然报告旧版本，可能是镜像写进了错误的槽（factory 而不是 user）。请确认使用的是本 Release 里的 user 镜像文件。
- 升级后请做您常用的采集测试。主从同步方面，本仓库的流程（`sync_acc_run.py`，见 README）是用这个镜像测试过的。

## 7. 说明

- `.rpd` 和 `.map` 是一对，不要混用不同版本的文件。
- 工具里还有其他页面（USB 固件升级、SD 卡升级、DLL 维护），本次升级用不到。
- 工具包里的 `qhyccd.dll`、`tbb.dll`、`winusb.dll` 来自该工具自己的编译目录。

## 8. 本 Release 的验证

2026-11-04 至 11-05，用本 Release 的文件和工具，通过 USB 升级了两台 QHY600M Pro ERIS 相机（主机 ac14、从机 8765）：

| 步骤 | 结果 |
|---|---|
| 升级前 | 两台：26-9-24，subversion1 0x80，subversion2 0x12，板型 2；工具日志 `128-18-54` |
| 第 1 台和第 2 台升级（依次进行） | 均为 `Upgrade Done`，分别用 23 分 10 秒和 23 分 06 秒 |
| 断电上电后 | 两台：26-9-24、0x80、0x12、板型 2，用户镜像模式启动 |
| USB 主从同步出图（16bit，全幅和 bin 3×3，各 10 帧） | 升级前、第 1 台升级后、第 2 台升级后：主从各 10/10 帧，序号 2~11 齐全，无丢帧 |

写入的镜像与相机原来运行的版本相同（0x12），所以版本号不会变化；测试说明本 Release 的文件和工具能写入可用的镜像，升级后出图和同步功能正常。PCIe 方式不在本次测试范围内。

仓库的测试 demo 也在升级后的相机上运行过，使用的是仓库的全新克隆（提交 0b9a06e）：

| Demo | 结果 |
|---|---|
| `sync_acc_run.py`，USB 主从，16bit，全幅和 bin 3×3 各 10 帧 | 主从各 10/10 帧，序号 2~11 齐全，无丢帧 |
| `e2e_test.py`（采集 GUI 真机端到端：扫描、连接、Live、同步压住/放行、退出再进入） | 22 项通过 20 项；2 项失败是 GUI 默认的 burst 起始寄存器值（1）与预设文件（0）不一致，是已知的默认值差异 |
