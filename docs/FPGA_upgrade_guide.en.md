# QHY600M Pro ERIS FPGA Upgrade Guide (sub2 = 0x12)

> **Closed-loop tested:** the firmware upgrade (files and tool of this release) and the capture test demo were tested on two QHY600M Pro ERIS cameras over USB and PCIe (the upgrade itself over USB). See section 8.

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

## 2. Tested configuration

The image supports master/slave synchronized shooting over USB and over PCIe/fibre (one-way trigger from the master to the slave). The tests in section 8 used these settings:

| Setting | Value |
|---|---|
| Bit depth, gain, exposure | 16 bit, gain 50, exposure 10 ms |
| Cooling | Off |
| Capture window | burst-start = 1, burst-end = n+2; valid frames are the sequence numbers 2 to n+1 |
| Frame numbers | Read from the `seq_number` of the GPS frame header (GPS on) |
| USB | Transfer traffic 55 for full frame and 30 for bin 3×3 |
| PCIe/fibre | Camera default traffic; GPS switched on after Live; burst padding 8192 (preset "Burst补包_PCIe光纤 (patch 8192)") |

See the PCIe and USB reports in `docs/` for the data of the synchronization tests.

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
5. Click **Scan (扫描)**, wait for the "SCAN DONE" message, and select the camera in the device list (a "connect success" message follows). Click **Read Version** and note the version shown before the upgrade. The log line `Transmission board version : 128-18-54` means subversion1 = 128 (0x80, user image) and subversion2 = 18 (0x12); the third number is not used in this guide.
6. Click **UPGRADE USER**.
7. The tool asks for two files, one after the other:
   - first the map file: select `remoteupdate_user_rpd.map` (it gives the end address of the user image);
   - then the image: select `remoteupdate_user_rpd_auto.rpd`.
8. Wait until the erase and the write finish. The log window and the progress bar show the progress; the erase of 85 sectors takes about 1 minute and the write about 22 minutes. A successful upgrade ends with lines like:
   ```
   Upgrade Done
   Upgrade time: 00:23:10
   Now we try to run the new firmware
   Transmission board data : 26-9-24
   The camera was boot on the user firmware image mode
   ```
   Right after the write the tool reads the version again; the last number may show 0 (for example `128-18-0`) until the camera has been power-cycled. This is expected.
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

## 8. Verification of this release

The files of this release were used to upgrade two QHY600M Pro ERIS cameras (master ac14, slave 8765) over USB with the tool of this release, on 2026-11-04 and 2026-11-05:

| Step | Result |
|---|---|
| Before the upgrade | Both cameras: 26-9-24, subversion1 0x80, subversion2 0x12, board type 2; tool log `128-18-54` |
| Upgrade of camera 1 and of camera 2 (one after the other) | `Upgrade Done`, 23 min 10 s and 23 min 06 s |
| After a power cycle | Both cameras: 26-9-24, 0x80, 0x12, board type 2, boot in user image mode |
| USB master/slave synchronized capture (16 bit, full frame and bin 3×3, 10 frames each) | Before the upgrade, after camera 1 and after camera 2: master and slave each 10 of 10 frames, sequence numbers 2 to 11 complete, no frame lost |

The image written is the same version as the one the cameras were already running (0x12), so the version numbers do not change; the test shows that the files and the tool write a working image and that capture and synchronization still work afterwards. The firmware upgrade itself is done over USB.

The test demo of the repository was also run on the upgraded cameras, from a fresh clone of the repository (commit 0b9a06e):

| Demo | Result |
|---|---|
| `sync_acc_run.py`, USB master/slave, 16 bit, full frame and bin 3×3, 10 frames each | Master and slave 10 of 10 frames, sequence numbers 2 to 11 complete, no frame lost |
| `e2e_test.py` (capture GUI with real cameras: scan, connect, live, sync hold and release, exit and re-enter) | 20 of 22 checks pass; the 2 failing checks compare the GUI default of the burst-start register (1) with the preset file (0), a known difference in defaults |

The same repository clone was also run with the cameras connected over PCIe/fibre after the upgrade:

| Check | Result |
|---|---|
| FPGA version read over PCIe | Both cameras: 26-9-24, subversion1 0x80, subversion2 0x12, board type 2 |
| `sync_acc_run.py`, PCIe master/slave, 16 bit, full frame and bin 3×3, 10 frames each | Master and slave 10 of 10 frames, sequence numbers 2 to 11 complete, no frame lost |
| `sync_acc_run.py`, PCIe master/slave, 16 bit, full frame, 100 frames | Master and slave 100 of 100 frames, sequence numbers 2 to 101 complete, no frame lost |
