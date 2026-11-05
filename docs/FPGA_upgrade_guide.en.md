# QHY600M Pro ERIS FPGA Upgrade Guide (sub2 = 0x12)

This guide describes the FPGA user image and the upgrade tool published in the GitHub Release `qhy600-fpga-0x12-20261012`.

## 1. What is released

| Asset | Content |
|---|---|
| `QHY600_FPGA_26-09-24_sub2-0x12.zip` | FPGA user image: `remoteupdate.jic`, `remoteupdate_user_rpd_auto.rpd`, `remoteupdate_user_rpd.map`, `SHA256SUMS.txt`, `README.txt` |
| `QHY_UPDATE_TOOL_win-x64.zip` | Upgrade tool QHYCCD Upgrade Tool V260518 (Windows, x64): `QHY_UPDATE_TOOL.exe` and the libraries it needs |
| `SHA256SUMS.txt` | Checksums of the two zip files |

Version of the image, as read from the cameras that run it:

| Field | Value |
|---|---|
| FPGA date | 26-09-24 |
| subversion1 | 0x80 (user image) |
| subversion2 | 0x12 |
| Board type | 0x02 (ERIS board, QHY600M Pro ERIS) |

Built from the project `ERIS_455_NIOS_1`, output package `remoteupgrade_20261012_09_40_49`.

## 2. About this version

The following comes from the debug records of the multi-camera synchronization work and is not a complete change list:

- The image supports master/slave synchronized shooting over USB and over PCIe/fibre (one-way trigger from the master to the slave).
- Version 0x0F added write gating and a tail flush in the PCIe transmit path (`PCIE/bypass_to_xgmii.v`). Versions 0x10 to 0x12 are diagnostic builds that try to overlay a frame-counter pattern into the image stream. In the tests the overlay did not appear in the buffer delivered by the SDK, so frame numbers are read from the GPS frame header (`seq_number`) instead.
- Known limitation: with a finite burst window on PCIe, a few frames at the head of the window may not be delivered (for example 28 of 30 with burst padding 8192). The cause was not fully resolved. See the PCIe report in `docs/` for the data of the synchronization tests.

## 3. Before you start

- Check the board: the upgrade is meant for the ERIS board (board type 0x02). Do not use it on other camera types.
- **The remote upgrade works over USB only.** A camera that is connected over PCIe/fibre cannot be upgraded with the tool (there is no flash write command on that path). Connect the camera with a USB cable.
- Close other programs that use the camera.
- Do not power off the camera or unplug the cable while the upgrade runs. Plan about 20 minutes per camera for the remote upgrade.
- Check the files against `SHA256SUMS.txt` before you start.

## 4. Method A: remote upgrade with the tool (USB)

1. Unzip `QHY_UPDATE_TOOL_win-x64.zip` into a folder. Keep `qhyccd.dll`, `tbb.dll` and `winusb.dll` in the same folder as `QHY_UPDATE_TOOL.exe`. The QHY camera USB driver must be installed. The tool needs .NET Framework 4.8.
2. Unzip `QHY600_FPGA_26-09-24_sub2-0x12.zip`.
3. Connect one camera by USB and start `QHY_UPDATE_TOOL.exe`.
4. Open the page **Main FPGA upgrade (主控FPGA升级)** in the sidebar.
5. Click **Scan (扫描)** and select the camera. Click **Read Version** and note the version shown before the upgrade.
6. Click **UPGRADE USER**.
7. The tool asks for two files, one after the other:
   - first the map file: select `remoteupdate_user_rpd.map` (it gives the end address of the user image);
   - then the image: select `remoteupdate_user_rpd_auto.rpd`.
8. Wait until the erase and the write finish. The log window shows the progress. A successful upgrade ends with lines like:
   ```
   Upgrade Done
   Verify summary: ... mismatches=0
   The camera was boot on the user firmware image mode
   ```
9. **Power-cycle the camera** (switch the power off, then on).
10. Start the tool again, scan and click **Read Version**. Right after power-up the registers can return 0 or old values; read again after a few seconds. The expected result is the version of section 1.
11. Repeat for the other camera. Upgrade the cameras one after the other.

## 5. Method B: JTAG programming with the JIC file

Use this method if you have a USB-Blaster and Quartus Prime Pro 18.1 (Quartus Programmer).

1. Connect the USB-Blaster to the camera's JTAG header and the camera power.
2. Program the **JIC file** (not an SOF file). The project uses a dual-image design: after a reload the FPGA loads the image from flash and overwrites anything you load by SOF.
   ```
   quartus_pgm -c "USB-Blaster [USB-0]" -m jtag -o "pvbi;remoteupdate.jic"
   ```
3. Programming takes about 3 minutes. Do not run two USB-Blasters at the same time; program the cameras one after the other.
4. **Power-cycle the camera.** The new image is loaded only after the power-up.
5. Read the version as in step 10 of method A.
6. Judge by the programmer's output, not only by the word "successful": a real programming run shows the configuration steps and takes minutes. A run that finishes in seconds did not program the device.

## 6. Check the result

- FPGA date 26-09-24, subversion1 0x80, subversion2 0x12, board type 0x02.
- If the camera still reports an older version after two power cycles, the image was probably written to the wrong slot (factory instead of user). Check that you used the user image files of this release.
- After the upgrade, run your usual capture test. For the master/slave synchronization the procedure of the repository (`sync_acc_run.py`, see the README) was tested with this image.

## 7. Notes

- The `.rpd` and `.map` files belong together; do not mix files of different builds.
- The tool contains other pages (USB firmware upgrade, SD card upgrade, DLL maintenance). They are not needed for this upgrade.
- The tool package contains `qhyccd.dll`, `tbb.dll` and `winusb.dll` from the tool's own build folder.
