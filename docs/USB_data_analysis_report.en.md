# USB Synchronized Capture Accuracy Test: Data Analysis (Error and Frame Loss)

> Scope: formal data set `20261103_usb` (15 groups each for 8 bit and 16 bit), plus the USB repeat tests of 11/04 (power cycling and cooling on/off, 7 groups). Master ac14 (shown as a7c0 under another SDK build), slave 8765 (shown as 847c).
> Statistics rule: clock readings come from automatic text recognition (OCR) plus sequence-constrained repair. Error statistics use frame pairs whose master − slave delta (Δ) is within ±100 ms. Pairs outside that range have readings that do not fit the neighbouring frames; they are classed as recognition anomalies and are not included in the error statistics (their count is given in Section 2). All numbers in the tables are computed directly from the readings, with no manual editing. Reading problems found by checking original images are listed in Section 3.5.
> Script: `usb_pure_analysis.py`. Results: `usb_pure_results.json`. Figures: `fig_usb_8bit_hist.png`, `fig_usb_16bit_hist.png`.

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

- Both cameras look at the same monitor, which shows a millisecond clock page. The two cameras view it from different angles, so the clock sits at a different position in each image.
- The monitor refreshes at 60 Hz, so the smallest step the clock can show is one refresh period (16.7 ms).
- The on-screen clock shows UTC+8. For this 11/03 daytime USB data the folder time stamps are also UTC+8 and agree with the screen clock.
- Δ = clock reading in the master image − clock reading in the slave image. A negative Δ means the master exposed earlier than the slave.
- Clock readings are obtained by OCR and then repaired using the time line of neighbouring frames (Sections 3.4 and 3.5).

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
- The picture composition is similar to the PCIe test of the evening of 11/03 (same monitor).

## 2. Data Overview

| Item | 8 bit | 16 bit |
|---|---|---|
| Formal groups | 15 | 15 |
| Captured pairs | 231 | 231 |
| Pairs with readings | 222 | 191 |
| Used in this analysis (\|Δ\| ≤ 100 ms) | 213 | 165 |
| Excluded (\|Δ\| > 100 ms) | 9 | 26 |

### Captured pairs versus pairs used in statistics

| Group | 8 bit captured | 8 bit read | 8 bit used | 16 bit captured | 16 bit read | 16 bit used |
|---|---|---|---|---|---|---|
| G1 (10 single-frame groups) | 10 | 10 | 8 | 10 | 10 | 7 |
| G2 | 10 | 9 | 9 | 10 | 8 | 7 |
| G3 | 100 | 100 | 97 | 100 | 78 | 66 |
| G4 | 1 | 1 | 1 | 1 | 1 | 1 |
| G5 | 10 | 10 | 8 | 10 | 9 | 8 |
| G6 | 100 | 92 | 90 | 100 | 85 | 76 |
| Total | 231 | 222 | 213 | 231 | 191 | 165 |

- Captured pairs are the frames actually saved in each group (one master and one slave image count as a pair). All are present and no frames were lost (Section 4).
- "Read" is lower than "captured" and "used" is lower than "read" because the automatic recognition gave no usable time, or the reading did not fit the time line; no image is missing. G3 of 16 bit has the most frames without a time reading (78 pairs read).
- The "pairs" of each group are therefore pairs with a time reading. The sample is smaller than what was captured; G1, G2, G4 and G5 have very few samples and are for reference only.

## 3. Error Analysis

### 3.1 Results per group (Δ = master − slave, in ms)

"Captured pairs" is the number of master/slave pairs actually saved. "Valid pairs" is the number of pairs with both times read and \|Δ\| ≤ 100 ms, used in the statistics below.

**8 bit**

| Group | Captured pairs | Valid pairs | Median | Mean | Std. dev. | Robust σ (MAD) | 5%–95% | Min–Max |
|---|---|---|---|---|---|---|---|---|
| G1 | 10 | 8 | −50.0 | −49.1 | 2.5 | 0.0 | −50 to −46 | −50 to −43 |
| G2 | 10 | 9 | −53.0 | −53.8 | 15.2 | 17.8 | −71 to −31 | −74 to −29 |
| G3 | 100 | 97 | −50.0 | −50.3 | 10.5 | 0.0 | −70 to −41 | −82 to +13 |
| G4 | 1 | 1 | −30.0 | n/a | n/a | n/a | n/a | n/a |
| G5 | 10 | 8 | −33.5 | −23.2 | 32.9 | 9.6 | −44 to +29 | −45 to +56 |
| G6 | 100 | 90 | −34.0 | −32.6 | 24.3 | 10.4 | −67 to +21 | −80 to +56 |
| **Full frame G1–G3** | 120 | 114 | **−50.0** | −50.5 | 10.5 | **0.0** | −70 to −39 | −82 to +13 |
| **Bin 3 G4–G6** | 111 | 99 | **−34.0** | −31.8 | 24.9 | **8.9** | −65 to +26 | −80 to +56 |

**16 bit**

| Group | Captured pairs | Valid pairs | Median | Mean | Std. dev. | Robust σ (MAD) | 5%–95% | Min–Max |
|---|---|---|---|---|---|---|---|---|
| G1 | 10 | 7 | −15.0 | −29.4 | 31.1 | 31.1 | −72 to +2 | −76 to +6 |
| G2 | 10 | 7 | −67.0 | −65.0 | 9.3 | 7.4 | −74 to −52 | −75 to −52 |
| G3 | 100 | 66 | −66.0 | −61.0 | 17.0 | 5.9 | −71 to −24 | −91 to +8 |
| G4 | 1 | 1 | −91.0 | n/a | n/a | n/a | n/a | n/a |
| G5 | 10 | 8 | −52.0 | −53.4 | 4.8 | 0.7 | −61 to −50 | −65 to −50 |
| G6 | 100 | 76 | −50.0 | −45.4 | 23.6 | 4.4 | −75 to −1 | −100 to +51 |
| **Full frame G1–G3** | 120 | 80 | **−66.0** | −58.6 | 20.0 | **5.9** | −72 to −7 | −91 to +8 |
| **Bin 3 G4–G6** | 111 | 85 | **−50.0** | −46.7 | 23.0 | **4.4** | −76 to −1 | −100 to +51 |

Key points:
- 8 bit: the full-frame median is −50 ms (−3 refresh periods) and the bin 3 median is −34 ms (−2 periods).
- 16 bit: the full-frame median is −66 ms (−4 periods) and the bin 3 median is −50 ms (−3 periods).
- At the same resolution 16 bit is one refresh period (about 16 to 17 ms) larger than 8 bit. A difference of one period is close to the resolution of the screen method, but 8 bit G3 has 97 pairs with a robust σ of 0, and 16 bit G3 has 66 pairs with a robust σ of 5.9, so the samples are not small and the difference does not look like reading noise.
- G1, G2, G4 and G5 have very few samples (1 to 10 pairs) and are for reference only. The conclusions rest mainly on the two 100-frame groups, G3 and G6.
- The standard deviation is much larger than the robust σ (for example 24.9 versus 8.9 for 8 bit bin 3), showing that a few outliers inflate the standard deviation. The robust σ is closer to the typical spread.

### 3.2 In refresh periods (figures `fig_usb_8bit_hist.png`, `fig_usb_16bit_hist.png`)

| Periods | −6 | −5 | −4 | −3 | −2 | −1 | 0 | +1 | +2 | +3 |
|---|---|---|---|---|---|---|---|---|---|---|
| 8 bit full frame (114) | n/a | 4 | 9 | **93** | 6 | 1 | n/a | 1 | n/a | n/a |
| 8 bit bin 3 (99) | n/a | 1 | 6 | 23 | **54** | 4 | 3 | 2 | 3 | 3 |
| 16 bit full frame (80) | n/a | 2 | **62** | 7 | 1 | 3 | 5 | n/a | n/a | n/a |
| 16 bit bin 3 (85) | 1 | 4 | 5 | **63** | 6 | n/a | 3 | n/a | 1 | 2 |

- Each row has one clear mode: 93 of 114 (82%) at −3 for 8 bit full frame, 54 of 99 (55%) at −2 for 8 bit bin 3, 62 of 80 (78%) at −4 for 16 bit full frame, and 63 of 85 (74%) at −3 for 16 bit bin 3.
- A single-frame Δ therefore falls almost entirely on integer multiples of 1 to 4 periods; the quantization is obvious. The true synchronization offset is better expressed as the median.

### 3.3 Drift within a group

| Mode · group | Pairs | Slope | Total over 100 frames |
|---|---|---|---|
| 8 bit G3 | 97 | −0.025 ms/frame | −2.5 ms |
| 8 bit G6 | 90 | +0.116 ms/frame | +11.6 ms |
| 16 bit G3 | 66 | +0.082 ms/frame | +8.2 ms |
| 16 bit G6 | 76 | −0.137 ms/frame | −13.7 ms |

The accumulated drift over 100 frames lies between −14 and +12 ms, below or close to one refresh period (16.7 ms), with both signs and no common trend. There is therefore no noticeable relative drift between master and slave within one burst.

### 3.4 Error sources (approximate breakdown)

1. **Screen quantization:** the clock advances in 16.7 ms steps, so the quantization error of a single reading is about ±8 ms (uniform, σ ≈ 4.8 ms).
2. **Ghosting:** the old and new refresh are superimposed, giving a reading uncertainty of about ±1 to 2 periods (±17 to 33 ms).
3. **OCR misreads:** some remain after the automatic repair.
4. **Camera and trigger jitter:** this cannot be separated out by the screen method; its upper bound is the total spread after the items above are combined.

The robust σ of the two full-frame modes is 0.0 ms (8 bit) and 5.9 ms (16 bit), so the cameras' own jitter is no larger than one refresh period; for bin 3 it is 8.9 and 4.4 ms.

### 3.5 Reading problems found by checking original images

For the USB data only two pairs of original images were checked (to verify the large intervals in Section 4.2):

| Group · sequence | Readings in the table (master / slave) | Readings from the original image | Remark |
|---|---|---|---|
| 16 bit G3·7 | 11:33:00.792 / 11:33:00.859 | 11:33:55.792 / 11:33:55.859 | Seconds 55 read as 00 in both cameras of the same frame; Δ is not affected (about −67 ms) |
| 16 bit G6·42 | 11:38:18.489 / 11:38:18.488 | about 11:38:13.429 / 11:38:13.459 (ghosting) | Seconds 13 read as 18 and the digits below the hundreds also wrong; Δ in the table is +1, Δ from the image is about −30 |

- This shows that the USB data also contains reading problems. Some errors move a single Δ away from the true value, and some affect master and slave together so that Δ is unchanged.
- The other USB pairs were not checked one by one. The statistics above use the automatic readings and were not modified, so the means and standard deviations are affected by individual misreads; the robust σ is affected less.

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

- **Average period over the whole burst** (frame 2 to frame 101, 99 intervals, from the sequence-number difference): 8 bit full frame G3 master 416.4 ms, slave 416.1 ms, bin 3 G6 master 303.4 ms, slave 303.8 ms; 16 bit full frame G3 master 800.2 ms, slave 800.2 ms, bin 3 G6 master 619.3 ms, slave 620.2 ms. Master and slave agree.
- This average checks that the sequence counter and the screen time agree. Frame loss itself is judged by the continuous sequence numbers and complete files above.
- **Adjacent-frame intervals** (from OCR readings; an interval is counted only when both frames were read):

| Mode · group · camera | Intervals | Median | Robust σ | Within ±2 periods of median | Deviating by > 200 ms |
|---|---|---|---|---|---|
| 8 bit G3 master | 99 | 417 ms | 1.5 | 87% | 0 |
| 8 bit G3 slave | 99 | 417 ms | 1.5 | 99% | 0 |
| 8 bit G6 master | 85 | 300 ms | 5.9 | 88% | 0 |
| 8 bit G6 slave | 85 | 300 ms | 4.4 | 87% | 0 |
| 16 bit G3 master | 62 | 800 ms | 0.7 | 84% | 6 |
| 16 bit G3 slave | 62 | 800 ms | 1.5 | 77% | 6 |
| 16 bit G6 master | 73 | 617 ms | 5.9 | 79% | 4 |
| 16 bit G6 slave | 73 | 617 ms | 5.9 | 79% | 4 |

- In 16 bit the intervals deviating by more than 200 ms (6 in G3, 4 in G6) all occur at the same frame pair for master and slave, and the sequence numbers are continuous. Two of them were checked (16 bit G3 sequence 6→7 and G6 sequence 41→42); the original images show a misread seconds digit (Section 3.5), not a real change of the capture interval. The others were not checked one by one.
- The median intervals of master and slave differ by 0 to 3 ms, so both cameras ran at the same cadence.

### 4.3 Attempt level

- All 30 formal groups are complete, and the 8 bit directory has no extra failed attempts.
- G1_01 of 16 bit was re-captured. In the first capture (11:24) and in the cleanup retry that followed, the sequence numbers saved for master and slave did not match (master seq 22, slave seq 18, no common number; stale frames). Both were archived in `archive_bad_g101`, together with one warm-up capture. The third capture (14:30) has sequence number 2 for both cameras and is the one used. The status file entry for G1_01 still points to the tag of the first capture and was not updated; this analysis uses the one group folder that exists for it.
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
- The offset of this set is **positive** (about +14 to +35 ms), whereas the 11/03 daytime USB data is **negative** (−34 to −66 ms). The same sign change was seen in the PCIe test. The camera position was adjusted between the two tests, which coincides with the sign change in time, but the mechanism has not been verified.

## 6. Conclusions

1. **Synchronization offset (11/03 daytime, before the camera position was adjusted):** 8 bit full frame median −50 ms, bin 3 −34 ms; 16 bit full frame −66 ms, bin 3 −50 ms. At the same resolution 16 bit is about one refresh period larger than 8 bit.
2. **Within a burst:** drift over 100 frames is between −14 and +12 ms; there is no noticeable relative drift.
3. **Frame loss:** in the 30 formal groups (924 frames) the sequence numbers are continuous, the files are complete and there are no duplicate frames, so no frames were lost. The frame period is stable: about 417 ms (full frame) and 300 ms (bin 3) for 8 bit, about 800 ms and 617 ms for 16 bit, identical for master and slave.
4. **Repeat tests:** three power cycles left the offset unchanged, and cooling on/off has no stable effect; after the camera position was adjusted the offset is about +14 to +35 ms.
5. **Measurement accuracy:** the smallest step of the screen method is 16.7 ms and the reading error of a single frame is about ±1 to 2 periods. A single-frame Δ should not be treated as an exact value; the median is more reliable.

## 7. Limitations

- G1, G2, G4 and G5 have very few samples and are for reference only; 16 bit G3 has only 66 valid pairs and G6 has 76.
- The automatic recognition still leaves misreads. Only two pairs of USB original images were checked (Section 3.5), so the means and standard deviations in the statistics are affected.
- The 11/03 USB capture script had no cooling-off step, so the cooling state was not controlled; gain and exposure are recorded as the script defaults and were not read back each time.
- The camera position was adjusted between the 11/03 USB data and the 11/04 repeat tests; the two periods have opposite signs and the mechanism has not been verified.
