# PCIe Synchronized Capture Accuracy Test: Data Analysis (Error and Frame Loss)

> Scope: formal data set `20261103_pcie16_r3`, 16 bit, master a7c0 (shown as ac14 under another SDK build), slave 847c (shown as 8765).
> Statistics rule: clock readings come from automatic text recognition (OCR) plus sequence-constrained repair. Error statistics use frame pairs whose master − slave delta (Δ) is within ±100 ms. Pairs outside that range have readings that do not fit the neighbouring frames; they are classed as recognition anomalies and are not included in the error statistics (their count is given in Section 2). All numbers in the tables are computed directly from the readings, with no manual editing. Residual misreads found by checking the original images are listed in Section 3.5.
> Script: `pcie_pure_analysis.py`. Results: `pcie_pure_results.json`, `cad_robust.json`. Figures: `fig_pcie_hist.png`, `fig_pcie_drift.png`.

## 1. Test Conditions and Groups

### 1.1 Equipment and synchronization

| Item | Description |
|---|---|
| Cameras | Two QHY600M cameras on PCIe fibre links. Master a7c0 (ac14 under another SDK build), slave 847c (8765). |
| Trigger | One-way trigger from master to slave; software cannot swap the roles. |
| Sync procedure | Same as the capture GUI: start Live, wait for the first frame, turn GPS on, set the master/slave sync registers (master configuration holding IDLE, slave configuration, burst patch 8192), then release the trigger from the master. |
| Acquisition | One separate capture subprocess per camera (each owns its SDK resources); the main process only writes the sync registers. |
| Pairing | GPS in the frame header is on; master and slave frames are paired by the header sequence number (`seq_number`). The time and latitude/longitude fields in the header are 0 and are not used for timing. |

### 1.2 Parameters common to all groups

| Parameter | Value |
|---|---|
| Bit depth | 16 bit (only 16 bit was tested for PCIe) |
| Gain | 50 |
| Exposure | 10 ms |
| Cooling | Off. After initialization `MANULPWM=0` is written and `CURPWM=0.0` is read back; it is confirmed again after Live. True for all 15 formal groups. |
| Transfer traffic | The capture script did not write this parameter to the camera (log shows `traffic=None`), so the camera default was used. The `t055` and `t030` in the group names are nominal values from the test plan, not applied settings. |
| Capture window | burst-start = 1, burst-end = n+2. Valid frames are burst-start+1 to burst-end−1, i.e. saved sequence numbers 2 to n+1. |
| Frame period | About 540.5 ms (the same for full frame and bin 3, see Section 4.2). |

### 1.3 Target and reading method

- Both cameras look at the same monitor, which shows a millisecond clock page. The two cameras view it from different angles, so the clock sits at a different position in each image.
- The monitor refreshes at 60 Hz, so the smallest step the clock can show is one refresh period (16.7 ms).
- The clock shows UTC+8 time. During recognition it is converted using the time stamp of the saved folder and checked against the reading.
- Δ = clock reading in the master image − clock reading in the slave image. A negative Δ means the master exposed earlier than the slave.
- Clock readings are obtained by OCR and then repaired using the time line of neighbouring frames (see Sections 3.4 and 3.5).

### 1.4 Groups and capture conditions

| Group | Resolution | Image size | Frames per group | Repeats | Saved sequence numbers | Completed (local time) |
|---|---|---|---|---|---|---|
| G1 | Full frame 1×1 | 9600×6422 | 1 | 10 groups (G1_01 to G1_10) | 2 | 11/03 22:14 to 22:31 |
| G2 | Full frame 1×1 | 9600×6422 | 10 | 1 | 2 to 11 | 11/03 22:33 |
| G3 | Full frame 1×1 | 9600×6422 | 100 | 1 | 2 to 101 | 11/03 23:26 |
| G4 | Bin 3×3 | 3200×2140 | 1 | 1 | 2 | 11/03 23:28 |
| G5 | Bin 3×3 | 3200×2140 | 10 | 1 | 2 to 11 | 11/04 01:11 |
| G6 | Bin 3×3 | 3200×2140 | 100 | 1 | 2 to 101 | 11/04 01:14 |

- Master and slave each save the same number of frames, 231 pairs in total for the formal data (G1 10, G2 10, G3 100, G4 1, G5 10, G6 100).
- G1 to G3 are full frame and G4 to G6 are bin 3×3; each resolution was tested with 1, 10 and 100 frames.

### 1.5 Changes during the test

- **Camera position:** the cameras were repositioned between G1–G4 (evening of 11/03) and G5–G6 (early morning of 11/04); the monitor was not moved. The picture composition differs between the two periods: in G5 and G6 the clock position in the image and the background are different from G1–G4.
- **Master–slave offset:** Section 3 shows that Δ changes from about −50 ms to about +19 ms between the two periods. The change coincides with the camera adjustment in time, but the mechanism has not been verified.
- **Failed capture attempts:** besides the formal data, the `groups` directory contains several incomplete capture attempts (Section 4.3). They are not used in the statistics.

## 2. Data Overview

| Item | Value |
|---|---|
| Formal groups | 15 (G1 ×10 single-frame groups, G2 10 frames, G3 100 frames, G4 1 frame, G5 10 frames, G6 100 frames) |
| Formal frames per camera | 231 frames, 462 frames for both cameras |
| Pairs with readings | 183 (G1 8, G2 6, G3 70, G4 1, G5 4, G6 94) |
| Used in this analysis (\|Δ\| ≤ 100 ms) | 169 (G1 8, G2 4, G3 68, G4 1, G5 4, G6 84) |
| Excluded (\|Δ\| > 100 ms) | 14 (G2 2, G3 2, G6 10) |

### Captured pairs versus pairs used in statistics

| Group | Captured pairs | Pairs with both readings | Of which \|Δ\| ≤ 100 ms (used) | Reason pairs are missing |
|---|---|---|---|---|
| G1 (10 single-frame groups) | 10 | 8 | 8 | No time was read from the slave frame of G1_02 and G1_03 |
| G2 | 10 | 6 | 4 | No slave reading, or reading could not be repaired |
| G3 | 100 | 70 | 68 | Mainly no slave reading (19 frames), plus readings that could not be repaired |
| G4 | 1 | 1 | 1 | none |
| G5 | 10 | 4 | 4 | No slave reading (6 frames) |
| G6 | 100 | 94 | 84 | Readings that could not be repaired |
| Total | 231 | 183 | 169 | none |

- Captured pairs are the frames actually saved in each group (one master and one slave image count as a pair). All are present and no frames were lost (see Section 4).
- A missing pair means the automatic recognition produced no usable time; the image is not missing. For example, both pairs G1_02 and G1_03 exist and the slave clock is clearly readable (from the original images: G1_02 master 14:15:40.665, slave 14:15:40.712, difference about −47 ms; G1_03 master 14:17:10.729, slave 14:17:10.779, difference about −50 ms), but the automatic recognition returned nothing for those two slave frames. These two pairs are not included in the statistics below.
- The "pairs" in each group are therefore pairs with a time reading. The sample is smaller than what was captured, most noticeably for G2 and G5, which should be treated as reference only.
- The screen refreshes every 16.7 ms and the smallest step the clock can show is one "refresh period". Δ is also expressed in refresh periods below.

## 3. Error Analysis

### 3.1 Results per group (Δ = master − slave, in ms)

"Captured pairs" is the number of master/slave pairs actually saved. "Valid pairs" is the number of pairs with both times read and \|Δ\| ≤ 100 ms, used in the statistics below. The reasons for the difference are in Section 2.

| Group | Captured pairs | Valid pairs | Median | Mean | Std. dev. | Robust σ (MAD) | 5%–95% | Min–Max |
|---|---|---|---|---|---|---|---|---|
| G1 | 10 | 8 | −45.5 | −26.3 | 33.5 | 6.7 | −50 to +26 | −50 to +40 |
| G2 | 10 | 4 | −54.5 | −60.0 | 22.2 | 14.8 | −86 to −41 | −91 to −40 |
| G3 | 100 | 68 | −50.0 | −47.8 | 19.2 | 3.7 | −70 to −15 | −89 to +34 |
| G4 | 1 | 1 | −50.0 | n/a | n/a | n/a | n/a | n/a |
| G5 | 10 | 4 | +21.0 | +8.8 | 38.9 | 23.0 | −38 to +38 | −46 to +39 |
| G6 | 100 | 84 | +19.0 | +15.0 | 28.2 | 17.8 | −56 to +39 | −67 to +97 |
| **Early G1–G4** | 121 | 81 | **−50.0** | −46.3 | 21.9 | **4.4** | −70 to +1 | −91 to +40 |
| **Late G5–G6** | 110 | 88 | **+19.0** | +14.8 | 28.5 | **17.8** | −56 to +39 | −67 to +97 |

Key points:
- The early period (11/03) has a median of −50 ms and the late period (11/04) a median of +19 ms. The signs are opposite and differ by about 70 ms.
- G1, G2, G4 and G5 have very few samples (1 to 8 pairs) and are for reference only. The conclusions rest mainly on the two 100-frame groups, G3 and G6.
- The standard deviation is much larger than the robust σ (early period 21.9 versus 4.4), showing that a few outliers inflate the standard deviation. The robust σ is closer to the typical spread.

### 3.2 In refresh periods (figure `fig_pcie_hist.png`)

| Periods | −5 | −4 | −3 | −2 | −1 | 0 | +1 | +2 | +6 |
|---|---|---|---|---|---|---|---|---|---|
| Early (81) | 3 | 9 | **53** | 8 | 3 | 1 | 2 | 2 | n/a |
| Late (88) | n/a | 2 | 8 | n/a | 1 | 7 | **31** | **38** | 1 |

- Early period: 53 of 81 (65%) are exactly at −3 periods (−50 ms); −4 to −2 together account for 70 of 81 (86%).
- Late period: +1 and +2 periods together account for 69 of 88 (78%); another 8 pairs are at −3 and 7 at 0.
- A single-frame Δ therefore falls almost entirely on integer multiples of 1 to 3 periods; the quantization is obvious. The true synchronization offset is better expressed as the median.

### 3.3 Drift within a group (figure `fig_pcie_drift.png`)

| Group | Pairs | Slope | Total over 100 frames |
|---|---|---|---|
| G3 | 68 | −0.032 ms/frame | −3.2 ms |
| G6 | 84 | −0.045 ms/frame | −4.5 ms |

The accumulated drift over 100 frames is only 3 to 5 ms, less than one refresh period (16.7 ms), so there is no noticeable relative drift between master and slave within one burst.

### 3.4 Error sources (approximate breakdown)

1. **Screen quantization:** the clock advances in 16.7 ms steps, so the quantization error of a single reading is about ±8 ms (uniform, σ ≈ 4.8 ms).
2. **Ghosting:** the old and new refresh are superimposed, giving a reading uncertainty of about ±1 to 2 periods (±17 to 33 ms).
3. **OCR misreads:** some remain after the automatic repair.
4. **Camera and trigger jitter:** this cannot be separated out by the screen method; its upper bound is the total spread after the items above are combined.

Taken together, the robust σ of the early period is about 4.4 ms, so the cameras' own jitter is no larger than one refresh period; the late period has a robust σ of about 17.8 ms, roughly one period.

### 3.5 Known residual misreads in this data

Checking the original images one by one showed that the automatic readings of the pairs below do not match the images. The statistics in the tables still use the automatic readings and were not modified:

| Group · sequence | Δ in the table | Δ read from the original image |
|---|---|---|
| G1_06 | +40 | −50 |
| G1_08 | −10 | about −50 |
| G1_09 | +1 | −50 |
| G3·32 | +9 | −51 |
| G3·51 | −140 | −50 |
| G3·61 | −146 | about −70 |
| G2·10 | −139 | about −61 |

- G3·51, G3·61 and G2·10 lie outside ±100 ms and were excluded. G1_06, G1_08, G1_09 and G3·32 lie within ±100 ms and are included in the tables above.
- The means and standard deviations in the tables therefore deviate from the true values; the G1 mean (−26.3) is affected most. The robust σ is affected less.
- In G6 two clusters of points have not been checked one by one: about 11 pairs on the high side (+97 to +123) and about 9 pairs on the low side (−44 to −67). The high cluster is known to come from a misread hundreds digit, with a true value of about +24 ms.

## 4. Frame Loss Analysis

### 4.1 Acquisition level (image files): no frame loss

| Check | Result |
|---|---|
| Frames per camera per group | All 15 groups match expectation (G1 1 each, G2 10, G3 100, G4 1, G5 10, G6 100; master and slave identical) |
| Sequence continuity | Sequence numbers in every group run from 2 to n+1, with no gaps and no extras |
| Status files | `any_drop` is False for all 15 groups; `M_missing` and `S_missing` are empty |
| Duplicate image files | Checked by content hash: 0 duplicates |
| Header sequence vs file sequence | All 462 frames agree; 0 mismatches |
| Image size | Full frame 9600×6422, bin 3 3200×2140, unchanged within a group |

### 4.2 Cadence: frame intervals are stable

- **Average period over the whole burst** (frame 2 to frame 101, 99 intervals): G3 master 540.6 ms, slave 540.6 ms; G6 master 541.4 ms, slave 540.5 ms.
- This average is computed from the sequence-number difference, to check that the sequence counter and the screen time agree. All four values are around 540.5 ms, so adjacent sequence numbers are indeed one period apart; there is no case of consecutive numbers with a two-period gap in between. Frame loss itself is judged by the continuous sequence numbers and complete files above.
- **Adjacent-frame intervals** (from OCR readings; an interval is counted only when both frames were read):

| Group · camera | Intervals | Median | Robust σ | Within ±2 periods of median | Deviating by > 200 ms |
|---|---|---|---|---|---|
| G3 master | 41 | 539 ms | 11.9 | 93% | 0 |
| G3 slave | 41 | 536 ms | 8.9 | 88% | 0 |
| G6 master | 87 | 539 ms | 16.3 | 74% | 2 |
| G6 slave | 87 | 534 ms | 23.7 | 77% | 2 (one of them > 900 ms) |

- In G6, 74 to 77% of the intervals are within ±2 periods. Most of the rest are caused by OCR errors (for example a misread hundreds digit) and are **not real frame loss**: the sequence numbers of that group are continuous, the files are complete and the average period is also 540 ms.
- The only interval above 900 ms is from the G6 slave and is also a reading problem; there is no corresponding missing sequence number.
- The median intervals of master and slave differ by 0 to 5 ms, so both cameras ran at the same cadence.

### 4.3 Attempt level: not every attempt succeeded

All 15 formal groups are complete, but 32 attempt folders remain in the `groups` directory. The main ones are:

| Group | Attempts | Frames (master/slave) |
|---|---|---|
| G1_01 | 3 | 0/0, 0/1, **1/1 (formal)** |
| G1_06 | 2 | 0/1, **1/1 (formal)** |
| G3 | 5 | 0/0, 0/100, 0/100, 0/100, **100/100 (formal)** |
| G4 | 2 | 0/0, **1/1 (formal)** |
| G5 | 6 | 0/0 ×4, 3/0, **10/10 (formal)** |
| G6 | 5 | 0/0 ×4, **100/100 (formal)** |

- In attempts 2 to 4 of G3 the master delivered 0 frames and the slave 100, meaning the master produced no image in those runs.
- Most of these attempts happened before the code and procedure fixes (GUI-style acquisition, cooling off, waiting for the first frame, and so on). They are not part of the formal data, and the specific reason for each failure was not checked individually in this report.
- A more accurate statement is therefore: **no frame of any formal capture was lost, but whole captures in which the master delivered no frames occurred several times early on.**

## 5. Conclusions

1. **Synchronization offset:** the early PCIe median is about −50 ms (−3 refresh periods) with a robust σ of about 4 ms; the late median is about +19 ms with a robust σ of about 18 ms. The two periods have opposite signs.
2. **Within a burst:** drift over 100 frames is only 3 to 5 ms; there is no noticeable relative drift.
3. **Frame loss:** in the 15 formal groups (462 frames) the sequence numbers are continuous, the files are complete and there are no duplicate frames, so no frames were lost. The frame period is about 540.5 ms and identical for master and slave.
4. **Measurement accuracy:** the smallest step of the screen method is 16.7 ms and the reading error of a single frame is about ±1 to 2 periods. A single-frame Δ should therefore not be treated as an exact value; the median is more reliable.

## 6. Limitations

- G1, G2, G4 and G5 have very few samples and are for reference only.
- This data still contains known residual misreads (Section 3.5) and two unchecked clusters of points in G6, which inflate the standard deviation.
- Only 183 of the 231 formal pairs have a time reading; the other frames had no clock read. This does not affect the frame-loss conclusion, but it reduces the sample size of the error statistics.
- The sign difference between the early and late periods is currently known only to coincide in time with the camera repositioning; the mechanism has not been verified.
