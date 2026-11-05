# LiuSir_Capture

QHYCCD sync demo: a capture GUI and test scripts for two QHY600M cameras in master/slave synchronized shooting, plus the analysis of the synchronization accuracy test.

QHYCCD 同步拍摄演示：双相机（QHY600M）主从同步拍摄的采集 GUI、测试脚本，以及同步精度测试的数据分析。

## Verification status / 验证状态

**The FPGA firmware upgrade and the software test demo have been tested in a closed loop (upgrade over USB; capture over USB and PCIe, 2026-11-04/05). / FPGA 固件升级和软件测试 demo 已做闭环测试（升级通过 USB；出图在 USB 和 PCIe 下，2026-11-04/05）。**

| Item / 项目 | Closed-loop test / 闭环测试 | Result / 结果 |
|---|---|---|
| FPGA upgrade files (`remoteupdate.jic`, `remoteupdate_user_rpd_auto.rpd`, `.map`) and upgrade tool V260518 / FPGA 升级文件和升级工具 | Two QHY600M Pro ERIS cameras were upgraded over USB with the files and the tool from the Release, one after the other, then power-cycled, version read, and captured again / 两台 QHY600M Pro ERIS 相机用 Release 里的文件和工具通过 USB 依次升级，上电后读版本并再次出图 | `Upgrade Done` on both (about 23 min each); after the power cycle both report 26-9-24, subversion1 0x80, subversion2 0x12, user image mode / 两台均升级完成（各约 23 分钟），上电后版本均为 26-9-24、0x80、0x12，用户镜像模式 |
| Test demo: capture script `sync_acc_run.py` / 测试 demo：采集脚本 | Run from a fresh clone of this repository on the upgraded cameras, USB master/slave, 16 bit, full frame and bin 3×3, 10 frames each / 用本仓库的全新克隆在升级后的相机上运行，USB 主从，16bit，全幅和 bin 3×3 各 10 帧 | Master and slave 10 of 10 frames each, sequence numbers 2 to 11 complete, no frame lost; the same result before the upgrade / 主从各 10/10 帧，序号 2~11 齐全，无丢帧；升级前结果相同 |
| Test demo: capture GUI `capture_gui.py` (`e2e_test.py`, real cameras) / 测试 demo：采集 GUI 端到端测试 | Same fresh clone, upgraded cameras, USB: scan, connect, live, sync hold/release, exit and re-enter / 同一全新克隆，升级后的相机，USB：扫描、连接、Live、同步压住/放行、退出再进入 | 20 of 22 checks pass; the 2 failing checks compare the GUI default of the burst-start register (1) with the preset file (0), a known difference in defaults, not a hardware failure / 22 项通过 20 项；2 项失败是 GUI 默认的 burst 起始寄存器值（1）与预设文件（0）不一致，是已知的默认值差异，不是硬件问题 |
| Test demo: capture script `sync_acc_run.py` over PCIe/fibre / 测试 demo：PCIe 下的采集脚本 | Same fresh clone, upgraded cameras connected over PCIe, master/slave, 16 bit / 同一全新克隆，升级后的相机通过 PCIe 连接，主从，16bit | FPGA version 26-9-24 / 0x80 / 0x12 on both; full frame and bin 3×3 10 of 10 frames, full frame 100 of 100 frames on master and slave, sequence numbers complete, no frame lost / 两台版本均为 26-9-24、0x80、0x12；全幅和 bin 3×3 各 10/10 帧，全幅 100/100 帧，主从序号齐全，无丢帧 |

Not covered / 未覆盖: the remote upgrade over PCIe/fibre (the upgrade is done over USB), the capture GUI end-to-end test over PCIe, and the master/slave time offset after the upgrade (not re-measured). / 通过 PCIe/光纤的远程升级（升级通过 USB 进行）、PCIe 下的采集 GUI 端到端测试、升级后的主从时间差（未重新测量）。

Details / 详情: [English](docs/FPGA_upgrade_guide.en.md#8-verification-of-this-release), [中文](docs/FPGA_upgrade_guide.zh-CN.md#8-本-release-的验证).

## Reports / 报告

PCIe (16 bit) synchronization accuracy: error and frame-loss analysis / PCIe（16bit）同步精度：误差与丢帧分析

- English: [docs/PCIe_data_analysis_report.en.md](docs/PCIe_data_analysis_report.en.md)
- 中文：[docs/PCIe_data_analysis_report.zh-CN.md](docs/PCIe_data_analysis_report.zh-CN.md)

USB (8 bit and 16 bit) synchronization accuracy and repeat tests / USB（8bit 和 16bit）同步精度与重复测试

- English: [docs/USB_data_analysis_report.en.md](docs/USB_data_analysis_report.en.md)
- 中文：[docs/USB_data_analysis_report.zh-CN.md](docs/USB_data_analysis_report.zh-CN.md)

PCIe summary / PCIe 摘要:

- Master − slave offset: median about −50 ms in the first period and about +26 ms in the second (the cameras were repositioned in between). / 主−从偏差：前段中位数约 −50 ms，后段约 +26 ms（期间调整过相机位置）。
- No frame loss in the 15 formal groups (462 frames): sequence numbers continuous, files complete. / 15 个正式组共 462 帧无丢帧：序号连续、文件齐全。
- Frame period about 540.5 ms for both cameras. / 两台相机帧周期均约 540.5 ms。
- Single-frame Δ is limited by the 60 Hz screen clock (16.7 ms per step); use the median. / 单帧 Δ 受 60 Hz 屏幕时钟（每格 16.7 ms）限制，请以中位数为准。

## Repository layout / 目录结构

| Path | Content |
|---|---|
| `capture_gui.py`, `host/` | Capture GUI and the per-camera capture worker / 采集 GUI 与逐相机采集子进程 |
| `sync_acc_run.py` | One synchronized burst capture, saves master/slave frames / 单次同步连拍采集 |
| `sync_acc_autonomous*.py`, `_launch_*.py` | Group scheduler (G1–G6) for the PCIe and USB tests / 分组（G1–G6）测试调度 |
| `analysis/ocr/run_pcie_ocr.py` | Clock recognition (OCR) and sequence-constrained repair / 时钟识别与序列修正 |
| `analysis/pcie/`, `analysis/usb/` | Error and frame-loss statistics scripts, results and figures / 误差与丢帧统计脚本、结果与图 |
| `results/pcie_pairs.csv`, `results/usb_pairs.csv`, `results/usb_repeat_pairs.csv` | Master/slave clock readings and Δ per frame pair (PCIe, USB, USB repeat tests) / 每对主从读数与 Δ（PCIe、USB、USB 重复测试） |
| `docs/` | Reports (English and Chinese) / 报告（中英文） |

## FPGA upgrade / FPGA 升级

FPGA user image (QHY600M Pro ERIS, 26-09-24, sub2 0x12) and the upgrade tool: [qhy600-fpga-0x12-20261012](https://github.com/LiuSirA/LiuSir_Capture/releases/tag/qhy600-fpga-0x12-20261012). Guide / 说明: [English](docs/FPGA_upgrade_guide.en.md), [中文](docs/FPGA_upgrade_guide.zh-CN.md). / QHY600M Pro ERIS 的 FPGA 用户镜像（26-09-24，sub2 0x12）和升级工具。

## Test data / 测试数据

The saved images are JPEG previews used only to read the on-screen clock. They are not stored in the repository; the PCIe data set is published as a GitHub Release: [pcie-data-20261103](https://github.com/LiuSirA/LiuSir_Capture/releases/tag/pcie-data-20261103). / 保存的图像是仅用于识别屏幕时钟的 JPEG 预览图，不放在仓库里，PCIe 数据以 GitHub Release 发布：[pcie-data-20261103](https://github.com/LiuSirA/LiuSir_Capture/releases/tag/pcie-data-20261103)。

The USB data sets are published as GitHub Releases: formal 8 bit / 16 bit groups [usb-formal-data-20261103](https://github.com/LiuSirA/LiuSir_Capture/releases/tag/usb-formal-data-20261103), 11/04 repeat tests [usb-repeat-tests-20261104](https://github.com/LiuSirA/LiuSir_Capture/releases/tag/usb-repeat-tests-20261104). / USB 数据以 GitHub Release 发布：11/03 的 8bit/16bit 正式组 [usb-formal-data-20261103](https://github.com/LiuSirA/LiuSir_Capture/releases/tag/usb-formal-data-20261103)，11/04 的重复测试 [usb-repeat-tests-20261104](https://github.com/LiuSirA/LiuSir_Capture/releases/tag/usb-repeat-tests-20261104)。

The CSV in `results/` is enough to check the statistics in the reports. / `results/` 里的 CSV 足以核对报告中的统计结果。

## Notes / 说明

- The scripts were written for one Windows test machine and still contain absolute local paths (for example `J:\Mywork\...`). Adjust them before running elsewhere. / 脚本针对一台 Windows 测试机编写，仍含本机绝对路径（如 `J:\Mywork\...`），在其他机器运行前需要修改。
- The QHY SDK libraries (`qhyccd.dll` and dependencies) are not included; install the QHY SDK/driver and place the DLL where `QHYCCD_DLL` points. / 不含 QHY SDK 库（`qhyccd.dll` 等），请自行安装 SDK/驱动，并让 `QHYCCD_DLL` 指向 DLL。
- `host/fpga_soft_modes.json` holds only the eight register presets used by the synchronized-capture test (USB and PCIe master/slave setup, burst patch, trigger release, exit). `host/fpga_soft_commands.py` only expands those presets. / `host/fpga_soft_modes.json` 只包含同步测试用到的 8 个寄存器预设（USB/PCIe 主从配置、Burst 补包、触发释放、退出同步），`host/fpga_soft_commands.py` 只负责展开这些预设。
- The helper that power-cycles the cameras through a smart plug is not included. Set `SYNC_ACC_NO_POWER=1` to run the PCIe scheduler without it. / 通过智能插座给相机断电上电的辅助脚本未包含，PCIe 调度可用 `SYNC_ACC_NO_POWER=1` 跳过。
- Requirements for the analysis: Python 3.12, NumPy, OpenCV, Pillow, pytesseract (with Tesseract OCR installed), matplotlib. / 分析依赖：Python 3.12、NumPy、OpenCV、Pillow、pytesseract（需安装 Tesseract OCR）、matplotlib。
