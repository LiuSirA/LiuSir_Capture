# USB Synchronized Capture Accuracy Test: Data Analysis (Error and Frame Loss)

> Scope: formal data set `20261103_usb` (15 groups each for 8 bit and 16 bit), plus the USB repeat tests of 11/04 (power cycling and cooling on/off, 7 groups). Master ac14 (shown as a7c0 under another SDK build), slave 8765 (shown as 847c).
> Reading method: the clock region of each camera is tilt-corrected and recognized (OCR), merged with the earlier recognition results and repaired with the time line of neighbouring frames. In the groups of 100 and 10 frames, a frame that is still more than 40 ms away from the time line, or has no reading, gets an estimate from that camera's own time line (marked `est`). Single-frame groups (G1, G4) have no time line; their clocks were read from the original images (marked `man`), and those that could not be decided are left out of the statistics. The statistics use frame pairs whose master − slave delta (Δ) is within ±100 ms. The reading method and the corrections are described in Section 3.5.
> Scripts: `usb_pure_analysis.py`, `usb_final_pairs.py`. Results: `usb_pure_results.json`, `usb_final_pairs.csv`. Figures: `fig_usb_8bit_hist.png`, `fig_usb_16bit_hist.png`.

## 1. Test Conditions and Groups

### 1.1 Equipment and synchronization

| Item | Description |
|---|---|
| Cameras | Two QHY600M cameras on USB. Master ac14 (a7c0), slave 8765 (847c). |
| Trigger | One-way trigger from master to slave; software cannot swap the roles. |
| Sync procedure | Start Live, set the master/slave sync registers (USB master one-key setting and USB slave one-key setting; the burst patch 32001 is included in them), then release the trigger from the master. |
| Acquisition | The same capture script as in the PCIe test (`sync_acc_run.py`, `--transport usb`). |
| Pairing | GPS in the frame header is on; master and slave frames are paired by the header sequence number (`seq_number`). The time and latitude/longitude fields in the header are 0 and are not used for timing. |

### 1.2 Common parameters

| Parameter | Value |
|---|---|
| Bit depth | 8 bit and 16 bit, one round each |
| Gain / exposure | Gain 50, exposure 10 ms (the defaults of the capture script; the logs of these runs have no settings echo, so the values were not read back one by one) |
| Transfer traffic | 55 for full frame and 30 for bin 3×3, both written to the camera. The two values come from the overnight test of 11/02; the climb and verify steps were skipped in this round. |
| Cooling | The 11/03 capture script had no "cooling off" step yet, so the cooling state was the camera default and was not controlled. The 11/04 repeat tests switched cooling off or on as planned (Section 5). |
| Capture window | burst-start = 1, burst-end = n+2. Valid frames are burst-start+1 to burst-end−1, i.e. saved sequence numbers 2 to n+1. |
| Frame period | 8 bit: about 417 ms full frame, about 300 ms bin 3; 16 bit: about 800 ms full frame, about 617 ms bin 3 (Section 4.2). |

### 1.3 Target and reading method

- Both cameras look at the same monitor, which shows a millisecond clock page. The two cameras view it from different angles, so the clock sits at a different position in each image, and the text is tilted in the image (about 15° for the master, about −8° for the slave).
- The monitor refreshes at 60 Hz, so the smallest step the clock can show is one refresh period (16.7 ms).
- The on-screen clock shows UTC+8. For this 11/03 daytime USB data the folder time stamps are also UTC+8 and agree with the screen clock.
- Δ = clock reading in the master image − clock reading in the slave image. A negative Δ means the master exposed earlier than the slave.
- How the readings were obtained and corrected is described in Section 3.5.

### 1.4 Groups and capture conditions

8 bit and 16 bit use the same groups, 15 formal groups each:

| Group | Resolution | Image size | Frames per group | Repeats | Saved sequence numbers | Traffic |
|---|---|---|---|---|---|---|
| G1 | Full frame 1×1 | 9600×6422 | 1 | 10 groups (G1_01 to G1_10) | 2 | 55 |
| G2 | Full frame 1×1 | 9600×6422 | 10 | 1 | 2 to 11 | 55 |
| G3 | Full frame 1×1 | 9600×6422 | 100 | 1 | 2 to 101 | 55 |
| G4 | Bin 3×3 | 3200×2140 | 1 | 1 | 2 | 30 |
| G5 | Bin 3×3 | 3200×2140 | 10 | 1 | 2 to 11 | 30 |
| G6 | Bin 3×3 | 3200×2140 | 100 | 1 | 2 to 101 | 30 |

- Each round has 231 frames per camera, 462 frames for both cameras; 924 frames for 8 bit and 16 bit together.
- Capture time (UTC+8, 11/03): 8 bit between 11:08 and 11:22, 16 bit between 11:24 and 11:39; G1_01 of 16 bit was re-captured at 14:30 (Section 4.3).

### 1.5 Conditions during the test

- The 8 bit and 16 bit data were captured one after the other on the same set-up during the daytime of 11/03. Whether the camera position changed during the capture was not recorded.
- When G1_01 of 16 bit was re-captured at 14:30, the picture composition already differed from the rest of the data taken between 11:24 and 11:39 (the clock position and the background changed in both cameras). The camera position was therefore adjusted between these two captures; the time of the adjustment was not recorded. That frame is not pooled with the other 16 bit data (Section 3.5).
- The picture composition of the other data is similar to the PCIe test of the evening of 11/03 (same monitor).

## 2. Data Overview

"Captured" is the number of master/slave pairs actually saved. The reading of each pair comes from recognition (obs), reading from the image (man) or a time-line estimate (est); some pairs are not used in the statistics.

| Item | 8 bit | 16 bit |
|---|---|---|
| Formal groups | 15 | 15 |
| Captured pairs | 231 | 231 |
| Read by recognition (both cameras) | 216 | 202 |
| Read from the image (single-frame groups) | 6 | 9 |
| Time-line estimate (at least one camera) | 9 | 18 |
| Not used in the statistics | 0 | 2 |
| Used in the statistics (\|Δ\| ≤ 100 ms) | 231 | 229 |

### Source of the readings per group

| Group | Captured | 8 bit obs | 8 bit man | 8 bit est | 8 bit used | 16 bit obs | 16 bit man | 16 bit est | 16 bit not used | 16 bit used |
|---|---|---|---|---|---|---|---|---|---|---|
| G1 (10 single-frame groups) | 10 | 5 | 5 | 0 | 10 | 0 | 9 | 0 | 1 | 9 |
| G2 | 10 | 10 | 0 | 0 | 10 | 10 | 0 | 0 | 0 | 10 |
| G3 | 100 | 97 | 0 | 3 | 100 | 83 | 0 | 17 | 0 | 100 |
| G4 | 1 | 0 | 1 | 0 | 1 | 0 | 0 | 0 | 1 | 0 |
| G5 | 10 | 8 | 0 | 2 | 10 | 9 | 0 | 1 | 0 | 10 |
| G6 | 100 | 96 | 0 | 4 | 100 | 100 | 0 | 0 | 0 | 100 |
| Total | 231 | 216 | 6 | 9 | 231 | 202 | 9 | 18 | 2 | 229 |

- All captured frames are present and no frames were lost (Section 4).
- The 2 pairs of 16 bit that are not used: G1_01 (re-captured, different conditions) and G4 (ghosting in the original image; the readings cannot be decided).
- The share of estimated frames is small (9 of 231 for 8 bit, 18 of 231 for 16 bit) but concentrated in 16 bit G3 (17 pairs); the reason is given in Section 3.5. Using only the pairs read by recognition changes the medians by no more than 1 ms (Section 3.1), so the estimates do not change the conclusions.

## 3. Error Analysis

### 3.1 Results per group (Δ = master − slave, in ms)

"Captured pairs" is the number of master/slave pairs actually saved. "Valid pairs" is the number of pairs used in the statistics (including readings from the image and estimates, but not the pairs left out).

**8 bit**

| Group | Captured pairs | Valid pairs | Median | Mean | Std. dev. | Robust σ (MAD) | 5%–95% | Min–Max |
|---|---|---|---|---|---|---|---|---|
| G1 | 10 | 10 | −50.0 | −50.0 | 0.0 | 0.0 | −50 to −50 | −50 to −50 |
| G2 | 10 | 10 | −53.5 | −56.2 | 12.5 | 5.9 | −76 to −42 | −83 to −35 |
| G3 | 100 | 100 | −50.0 | −51.8 | 7.7 | 0.0 | −70 to −43 | −82 to −36 |
| G4 | 1 | 1 | −33.0 | n/a | n/a | n/a | n/a | n/a |
| G5 | 10 | 10 | −36.0 | −35.6 | 7.9 | 7.4 | −44 to −23 | −45 to −18 |
| G6 | 100 | 100 | −34.0 | −37.9 | 13.2 | 3.7 | −60 to −21 | −86 to +1 |
| **Full frame G1–G3** | 120 | 120 | **−50.0** | −52.0 | 7.9 | **0.0** | −70 to −43 | −83 to −35 |
| **Bin 3 G4–G6** | 111 | 111 | **−34.0** | −37.6 | 12.8 | **5.9** | −57 to −20 | −86 to +1 |

**16 bit**

| Group | Captured pairs | Valid pairs | Median | Mean | Std. dev. | Robust σ (MAD) | 5%–95% | Min–Max |
|---|---|---|---|---|---|---|---|---|
| G1 | 10 | 9 | −65.0 | −61.0 | 6.8 | 3.0 | −67 to −50 | −67 to −50 |
| G2 | 10 | 10 | −67.5 | −68.5 | 2.1 | 0.7 | −72 to −67 | −72 to −67 |
| G3 | 100 | 100 | −64.0 | −62.0 | 5.6 | 4.4 | −68 to −50 | −76 to −49 |
| G4 | 1 | 0 | n/a | n/a | n/a | n/a | n/a | n/a |
| G5 | 10 | 10 | −52.0 | −52.9 | 4.8 | 2.2 | −61 to −48 | −65 to −47 |
| G6 | 100 | 100 | −50.0 | −46.5 | 9.8 | 1.5 | −54 to −31 | −82 to 0 |
| **Full frame G1–G3** | 120 | 119 | **−65.0** | −62.5 | 5.7 | **4.4** | −69 to −50 | −76 to −49 |
| **Bin 3 G4–G6** | 111 | 110 | **−50.0** | −47.1 | 9.7 | **3.0** | −55 to −32 | −82 to 0 |

Using only the pairs read by recognition (without readings from the image and estimates) gives the following; the medians differ from the tables above by no more than 1 ms:

| Mode · resolution | Pairs | Median | Robust σ | Std. dev. |
|---|---|---|---|---|
| 8 bit full frame | 112 | −50.0 | 0.0 | 8.1 |
| 8 bit bin 3 | 104 | −34.0 | 2.2 | 13.2 |
| 16 bit full frame | 93 | −66.0 | 1.5 | 5.8 |
| 16 bit bin 3 | 109 | −50.0 | 3.0 | 9.7 |

Key points:
- 8 bit: the full-frame median is −50 ms (−3 refresh periods) and the bin 3 median is −34 ms (−2 periods).
- 16 bit: the full-frame median is −65 ms (−4 periods) and the bin 3 median is −50 ms (−3 periods).
- At the same resolution 16 bit is about one refresh period (about 15 to 16 ms) larger than 8 bit. A difference of one period is close to the resolution of the screen method, but 8 bit and 16 bit full frame each have more than 100 pairs and a robust σ below 5 ms, so the difference does not look like reading noise.
- G1, G2, G4 and G5 have very few samples (1 to 10 pairs) and are for reference only. The conclusions rest mainly on the two 100-frame groups, G3 and G6.
- Compared with the earlier version of the recognition, the standard deviations are clearly smaller after the correction (for example 16 bit G3 dropped from 17.0 to 5.6), so the large spread before was mainly reading error; the medians hardly changed.

### 3.2 In refresh periods (figures `fig_usb_8bit_hist.png`, `fig_usb_16bit_hist.png`)

| Periods | −5 | −4 | −3 | −2 | −1 | 0 |
|---|---|---|---|---|---|---|
| 8 bit full frame (120) | 4 | 12 | **98** | 6 | n/a | n/a |
| 8 bit bin 3 (111) | 1 | 5 | 26 | **71** | 5 | 3 |
| 16 bit full frame (119) | 1 | **97** | 21 | n/a | n/a | n/a |
| 16 bit bin 3 (110) | 2 | 2 | **88** | 16 | 1 | 1 |

- Each row has one clear mode: 98 of 120 (82%) at −3 for 8 bit full frame, 71 of 111 (64%) at −2 for 8 bit bin 3, 97 of 119 (82%) at −4 for 16 bit full frame, and 88 of 110 (80%) at −3 for 16 bit bin 3.
- A single-frame Δ therefore falls almost entirely on integer multiples of 1 to 4 periods; the quantization is obvious. The true synchronization offset is better expressed as the median.

### 3.3 Drift within a group

| Mode · group | Pairs | Slope | Total over 100 frames |
|---|---|---|---|
| 8 bit G3 | 100 | −0.126 ms/frame | −12.6 ms |
| 8 bit G6 | 100 | +0.010 ms/frame | +1.0 ms |
| 16 bit G3 | 100 | +0.052 ms/frame | +5.2 ms |
| 16 bit G6 | 100 | −0.128 ms/frame | −12.8 ms |

The accumulated drift over 100 frames lies between −13 and +5 ms, below one refresh period (16.7 ms), with both signs and no common trend. There is therefore no noticeable relative drift between master and slave within one burst. The figures include estimated frames; an estimate comes from each camera's own time line and has little effect on the slope.

### 3.4 Error sources (approximate breakdown)

1. **Screen quantization:** the clock advances in 16.7 ms steps, so the quantization error of a single reading is about ±8 ms (uniform, σ ≈ 4.8 ms).
2. **Ghosting:** the old and new refresh are superimposed, giving a reading uncertainty of about ±1 to 2 periods (±17 to 33 ms); this is most obvious for frames close to a hundreds-of-milliseconds digit rollover (Section 3.5).
3. **Reading errors:** some may remain after the automatic correction, and the readings taken from the image of single-frame groups are also limited by ghosting.
4. **Camera and trigger jitter:** this cannot be separated out by the screen method; its upper bound is the total spread after the items above are combined.

The robust σ of the two full-frame modes is 0.0 ms (8 bit) and 4.4 ms (16 bit), so the cameras' own jitter is no larger than one refresh period; for bin 3 it is 5.9 and 3.0 ms.

### 3.5 Reading method and corrections

**(1) Tilt-corrected region recognition.** The clock text is tilted in the image of both cameras (about 15° for the master, about −8° for the slave), and the earlier automatic localisation returned no time for some frames (9 pairs in 8 bit and 40 pairs in 16 bit, mostly 16 bit G3). The clock region of each camera is now rotated to correct the tilt before recognition, merged with the earlier recognition results, and repaired with the time line of neighbouring frames. After this, 229 of 231 pairs (8 bit) and 222 of 231 pairs (16 bit) have a reading from recognition.

**(2) Why 16 bit G3 is hard to read around sequence 42 to 58.** The frame period of this group is about 800.2 ms, roughly 8 times 100 ms, so the position of each frame relative to the hundreds-of-milliseconds rollover moves by only about 0.2 ms per frame. The master was about 10 ms before the rollover at sequence 2, exactly at the rollover at sequence 49 (11:34:29.40) and 2 ms past it at sequence 60. In this stretch the master exposure always falls a few milliseconds around the rollover, and the old and new digits overlap on the screen (for example a 3 and a 4 in the hundreds digit, a 0 and a 9 in the tens digit). The automatic recognition read sequence 49 as `11:34:29.308`, giving Δ = −151 ms; on the master's time line this frame is at about 11:34:29.400, Δ ≈ −64 ms, in line with the group median. 8 bit bin 3 has a frame period of about 303 ms and shows a similar effect, but the position moves by 3.5 ms per frame, so fewer frames are affected.

**(3) Time-line estimates.** For groups of 10 frames or more, a straight line of time against sequence number is fitted for each camera (in the short groups both cameras share one frame period). A reading that is more than 40 ms from the line, or missing, is replaced by the value on the line and marked `est` in the `master` and `slave` columns of `usb_final_pairs.csv`. In 27 pairs at least one camera has an estimated value: 3 pairs in 8 bit G3, 2 in G5, 4 in G6, 17 in 16 bit G3 (sequence 50 to 86) and 1 in G5. The accuracy of an estimate is about ±8 ms (limited by the screen resolution).

**(4) Readings from the image for single-frame groups.** G1 (10 single-frame groups) and G4 have no time line; their clock regions were read one by one from the original images and marked `man`. The table lists the 15 pairs whose reading changed (where digits overlap, the clearer layer was read; the uncertainty is about 1 to 2 periods):

| Mode · group | Recognition (master/slave) | Read from the image (master/slave) | Δ of the image reading |
|---|---|---|---|
| 8 bit G1_02 | .599 / .606 (−7) | .556 / .606 | −50 |
| 8 bit G1_03 | .596 / .606 (−10) | .556 / .606 | −50 |
| 8 bit G1_05 | .180 / .223 (−43) | .173 / .223 | −50 |
| 8 bit G1_08 | 14:48.099 / 14:48.130 (−31) | 14:43.090 / 14:43.140 | −50 |
| 8 bit G1_10 | 26.949 / 27.990 (−1041) | 26.940 / 26.990 | −50 |
| 8 bit G4 | .346 / .340 (+6) | .324 / .357 | −33 |
| 16 bit G1_02 | .451 / .507 (−56) | .441 / .507 | −66 |
| 16 bit G1_03 | .291 / .331 (−40) | .291 / .351 | −60 |
| 16 bit G1_04 | .080 / .074 (+6) | .008 / .074 | −66 |
| 16 bit G1_05 | 27:48.924 / 27:48.932 (−8) | 27:43.924 / 27:43.974 to .991 (midpoint .983 used) | −50 to −67 (−59 used) |
| 16 bit G1_06 | .780 / .778 (+2) | .708 / .758 | −50 |
| 16 bit G1_07 | .808 / .758 (+50) | .708 / .758 | −50 |
| 16 bit G1_08 | 19.092 / 19.091 (+1) | 19.525 / 19.591 | −66 |
| 16 bit G1_09 | .598 / .654 (−56) | .591 / .658 | −67 |
| 16 bit G1_10 | .208 / .215 (−7) | .208 / .273 | −65 |

The slave reading 11:28:35.758 of 16 bit G1_06 was confirmed by a manual check. In 16 bit G1_05 the milliseconds of the slave have two overlapping layers (7 and 9 in the tens digit, 4 and 1 in the ones digit); a manual check puts the time between .974 and .991, at a digit rollover, and the midpoint .983 is used, Δ ≈ −59 ms (range −50 to −67 ms). The recognition had also read the seconds 43 as 48.

**(5) Pairs not used in the statistics.**
- 16 bit G1_01 was re-captured at 14:30 after the camera position had changed, and its picture composition differs from the rest of the data. The readings in the image are master 14:31:24.142 and slave 14:31:24.157, Δ −15 ms; they are kept as a record only and are not pooled into the 16 bit full-frame statistics.
- 16 bit G4 (recognition .309 / .400, Δ −91): the milliseconds of both cameras have a second overlapping layer, at a digit rollover. The clearer layer reads .309 for the master and .442 for the slave (the slave was also checked manually and is at a rollover), Δ −133 ms, which does not fit the other data of the same kind (Δ between −47 and −65 ms for 16 bit bin 3); ghosting is suspected, neither reading can be decided, and the pair is not used.

**(6) Limitation of the readings.** The recognition readings of the 100-frame groups were not checked one by one against the images. Estimated frames and readings from the image are marked in the result table, and the statistics are given both for all valid frames and for the frames read by recognition only (Section 3.1).

## 4. Frame Loss Analysis

### 4.1 Acquisition level (image files): no frame loss

| Check | Result |
|---|---|
| Frames per camera per group | All 30 groups of 8 bit and 16 bit match expectation (G1 1 each, G2 10, G3 100, G4 1, G5 10, G6 100; master and slave identical) |
| Sequence continuity | Sequence numbers in every group run from 2 to n+1, with no gaps and no extras |
| Status files | `any_drop` is False for all 30 groups; `M_missing` and `S_missing` are empty |
| Duplicate image files | Checked by content hash: 0 duplicates |
| Header sequence vs file sequence | All agree; 0 mismatches |
| Image size | Full frame 9600×6422, bin 3 3200×2140, unchanged within a group |

### 4.2 Cadence: frame intervals are stable

- **Average period over the whole burst** (frame 2 to frame 101, 99 intervals, from the sequence-number difference): 8 bit full frame G3 master 416.4 ms, slave 416.7 ms, bin 3 G6 master 303.5 ms, slave 303.8 ms; 16 bit full frame G3 master 800.2 ms, slave 800.2 ms, bin 3 G6 master 620.1 ms, slave 620.2 ms. Master and slave agree.
- This average checks that the sequence counter and the screen time agree. Frame loss itself is judged by the continuous sequence numbers and complete files above.
- **Adjacent-frame intervals** (only intervals between two frames both read by recognition; estimated frames are not used):

| Mode · group · camera | Intervals | Median | Robust σ | Within ±2 periods of median | Deviating by > 200 ms |
|---|---|---|---|---|---|
| 8 bit G3 master | 94 | 417 ms | 1.5 | 97% | 0 |
| 8 bit G3 slave | 94 | 417 ms | 1.5 | 100% | 0 |
| 8 bit G6 master | 92 | 300 ms | 3.0 | 96% | 0 |
| 8 bit G6 slave | 92 | 300 ms | 2.2 | 93% | 0 |
| 16 bit G3 master | 72 | 800 ms | 0.0 | 100% | 0 |
| 16 bit G3 slave | 72 | 800 ms | 0.0 | 100% | 0 |
| 16 bit G6 master | 99 | 617 ms | 1.5 | 98% | 0 |
| 16 bit G6 slave | 99 | 617 ms | 3.0 | 99% | 0 |

- The 10 large intervals seen in 16 bit in the earlier version (6 in G3, 4 in G6) were caused by misread seconds digits and no longer occur after the readings were corrected.
- The median intervals of master and slave differ by 0 to 1 ms, so both cameras ran at the same cadence.

### 4.3 Attempt level

- All 30 formal groups are complete, and the 8 bit directory has no extra failed attempts.
- G1_01 of 16 bit was re-captured. In the first capture (11:24) and in the cleanup retry that followed, the sequence numbers saved for master and slave did not match (master seq 22, slave seq 18, no common number; stale frames). Both were archived in `archive_bad_g101`, together with one warm-up capture. The third capture (14:30) has sequence number 2 for both cameras and is the one used; by then the camera position had been adjusted (Section 1.5). The status file entry for G1_01 still points to the tag of the first capture and was not updated; this analysis uses the one group folder that exists for it.
- A more accurate statement is therefore: **no frame of any formal capture was lost, but the first capture of 16 bit G1_01 had master and slave sequence numbers that did not match (stale frames), which was solved by the retry.**

## 5. USB Repeat Tests (Power Cycling and Cooling)

In the early morning of 11/04, seven 16 bit full-frame 100-frame groups (G3 conditions) were captured on the same set-up to answer two questions: does the offset change after each power cycle, and does switching the cooling on or off change it. Capture times of these data are stamped in machine local time (UTC−8); the on-screen clock shows UTC+8.

- The camera position had been adjusted before, so the clock sits at a different position in the images. Each camera was recognized with its own fixed clock region; the rest of the repair procedure is unchanged.
- sess1 to sess3 were each preceded by a power cycle through a smart plug; there was no power cycle between the four coolON/coolOFF groups. All 7 groups are USB, 16 bit, 1×1, exposure 10 ms, traffic 55, with 100 frames per camera and sequence numbers 2 to 101 complete.

| Round | Condition | Local time | Pairs with readings | Valid pairs | Median | Robust σ |
|---|---|---|---|---|---|---|
| sess1 | 1st round after power cycle, cooling off | 11/04 01:51 | 91 | 91 | +33 | 8.9 |
| sess2 | 2nd round after power cycle, cooling off | 11/04 01:55 | 98 | 98 | +33 | 5.9 |
| sess3 | 3rd round after power cycle, cooling off | 11/04 01:58 | 80 | 79 | +33 | 13.3 |
| coolON1 | cooling on | 11/04 02:08 | 88 | 88 | +14 | 17.8 |
| coolOFF2 | cooling off | 11/04 02:11 | 80 | 80 | +34.5 | 6.7 |
| coolON3 | cooling on | 11/04 02:15 | 96 | 96 | +17 | 1.5 |
| coolOFF4 | cooling off | 11/04 02:19 | 77 | 76 | +17 | 0.0 |

- After three consecutive power cycles the offset is +33 ms every time; it did not change.
- With cooling on the medians are +14 and +17, with cooling off +34.5 and +17. The ranges overlap, so no stable effect of cooling on the offset can be seen; the differences are within one to two refresh periods.
- The offset of this set is **positive** (about +14 to +35 ms), whereas the 11/03 daytime USB data is **negative** (−34 to −65 ms). The same sign change was seen in the PCIe test. The camera position was adjusted between the two tests (the re-captured frame of 16 bit G1_01 already shows a camera position different from the rest of the 11/03 data), which coincides with the sign change in time, but the mechanism has not been verified.
- The readings of these 7 groups are still the result of the earlier recognition; the tilt-corrected recognition and the time-line estimates of Section 3 were not applied to them, so the method is not exactly the same as in Section 3.

## 6. Conclusions

1. **Synchronization offset (11/03 daytime, before the camera position was adjusted):** 8 bit full frame median −50 ms, bin 3 −34 ms; 16 bit full frame −65 ms, bin 3 −50 ms. At the same resolution 16 bit is about one refresh period larger than 8 bit.
2. **Within a burst:** drift over 100 frames is between −13 and +5 ms; there is no noticeable relative drift.
3. **Frame loss:** in the 30 formal groups (924 frames) the sequence numbers are continuous, the files are complete and there are no duplicate frames, so no frames were lost. The frame period is stable: about 417 ms (full frame) and 300 ms (bin 3) for 8 bit, about 800 ms and 617 ms for 16 bit, identical for master and slave.
4. **Anomalous points:** the points with |Δ| > 100 ms and the large intervals seen before are reading problems: recognition failures caused by the tilted text, and overlapping digits when the exposure falls close to a hundreds-of-milliseconds rollover because the frame period is close to a multiple of 100 ms. After the readings were corrected no pair with a master–slave difference above 100 ms remains.
5. **Repeat tests:** three power cycles left the offset unchanged, and cooling on/off has no stable effect; after the camera position was adjusted the offset is about +14 to +35 ms.
6. **Measurement accuracy:** the smallest step of the screen method is 16.7 ms and the reading error of a single frame is about ±1 to 2 periods. A single-frame Δ should not be treated as an exact value; the median is more reliable.

## 7. Limitations

- G1, G2, G4 and G5 have very few samples and are for reference only. The readings of single-frame groups come from the images and have an uncertainty of about 1 to 2 periods where digits overlap; 16 bit G4 could not be decided and G1_01 has different conditions, so neither is used; the slave milliseconds of 16 bit G1_05 are only known to lie between .974 and .991, and the midpoint is used (±9 ms).
- In 9 (8 bit) and 18 (16 bit) pairs the time of one camera is a time-line estimate (most in 16 bit G3, 17 pairs). An estimate has an accuracy of about ±8 ms and is not a measured reading; using only the pairs read by recognition changes the medians by no more than 1 ms.
- The recognition readings of the 100-frame groups were not checked one by one against the images, so reading errors that were not found may remain.
- The 11/03 USB capture script had no cooling-off step, so the cooling state was not controlled; gain and exposure are recorded as the script defaults and were not read back each time.
- The camera position was adjusted between the 11/03 USB data and the 11/04 repeat tests; the two periods have opposite signs and the mechanism has not been verified; the readings of the 11/04 repeat tests are from the earlier recognition.
