# LiuSir_Capture

QHYCCD sync demo: a capture GUI and test scripts for two QHY600M cameras in master/slave synchronized shooting, plus the analysis of the synchronization accuracy test.

QHYCCD 同步拍摄演示：双相机（QHY600M）主从同步拍摄的采集 GUI、测试脚本，以及同步精度测试的数据分析。

## Reports / 报告

PCIe (16 bit) synchronization accuracy: error and frame-loss analysis / PCIe（16bit）同步精度：误差与丢帧分析

- English: [docs/PCIe_data_analysis_report.en.md](docs/PCIe_data_analysis_report.en.md)
- 中文：[docs/PCIe_data_analysis_report.zh-CN.md](docs/PCIe_data_analysis_report.zh-CN.md)

Summary / 摘要:

- Master − slave offset: median about −50 ms in the first period and about +19 ms in the second (the cameras were repositioned in between). / 主−从偏差：前段中位数约 −50 ms，后段约 +19 ms（期间调整过相机位置）。
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
| `analysis/pcie/` | Error and frame-loss statistics script, results and figures / 误差与丢帧统计脚本、结果与图 |
| `results/pcie_pairs.csv` | Master/slave clock readings and Δ per frame pair (PCIe) / 每对主从读数与 Δ |
| `docs/` | Reports (English and Chinese) / 报告（中英文） |

## Test data / 测试数据

The saved images are JPEG previews used only to read the on-screen clock. They are not stored in the repository; the PCIe data set is published as a GitHub Release (link to be added). / 保存的图像是仅用于识别屏幕时钟的 JPEG 预览图，不放在仓库里，PCIe 数据以 GitHub Release 发布（链接待补充）。

The CSV in `results/` is enough to check the statistics in the reports. / `results/` 里的 CSV 足以核对报告中的统计结果。

## Notes / 说明

- The scripts were written for one Windows test machine and still contain absolute local paths (for example `J:\Mywork\...`). Adjust them before running elsewhere. / 脚本针对一台 Windows 测试机编写，仍含本机绝对路径（如 `J:\Mywork\...`），在其他机器运行前需要修改。
- The QHY SDK libraries (`qhyccd.dll` and dependencies) are not included; install the QHY SDK/driver and place the DLL where `QHYCCD_DLL` points. / 不含 QHY SDK 库（`qhyccd.dll` 等），请自行安装 SDK/驱动，并让 `QHYCCD_DLL` 指向 DLL。
- `host/fpga_soft_modes.json` holds only the eight register presets used by the synchronized-capture test (USB and PCIe master/slave setup, burst patch, trigger release, exit). `host/fpga_soft_commands.py` only expands those presets. / `host/fpga_soft_modes.json` 只包含同步测试用到的 8 个寄存器预设（USB/PCIe 主从配置、Burst 补包、触发释放、退出同步），`host/fpga_soft_commands.py` 只负责展开这些预设。
- The helper that power-cycles the cameras through a smart plug is not included. Set `SYNC_ACC_NO_POWER=1` to run the PCIe scheduler without it. / 通过智能插座给相机断电上电的辅助脚本未包含，PCIe 调度可用 `SYNC_ACC_NO_POWER=1` 跳过。
- Requirements for the analysis: Python 3.12, NumPy, OpenCV, Pillow, pytesseract (with Tesseract OCR installed), matplotlib. / 分析依赖：Python 3.12、NumPy、OpenCV、Pillow、pytesseract（需安装 Tesseract OCR）、matplotlib。
