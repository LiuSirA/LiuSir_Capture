# PCIe Synchronized Capture Accuracy Test: Data Analysis (Error and Frame Loss)

> Scope: formal data set `20261103_pcie16_r3`, 16 bit, master a7c0 (shown as ac14 under another SDK build), slave 847c (shown as 8765).
> Reading method: the clock region of each camera is tilt-corrected and recognized (OCR), merged with the earlier recognition results and repaired with the time line of neighbouring frames. In the groups of 100 and 10 frames, a frame that is still more than 40 ms away from the time line, or has no reading, gets an estimate from that camera's own time line (marked `est`). Single-frame groups (G1, G4) have no time line; their clocks were read from the original images (marked `man`). The readings were checked against the original images on a sample, and those with a large deviation were corrected from the image (marked `chk`). The statistics use frame pairs whose master − slave delta (Δ) is within ±100 ms; this time all 231 pairs are within that range. The reading method and the corrections are described in Section 3.5.
> Scripts: `pcie_pure_analysis.py`, `pcie_final_pairs.py`. Results: `pcie_pure_results.json`, `pcie_final_pairs.csv`. Figures: `fig_pcie_hist.png`, `fig_pcie_drift.png`.

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

- Both cameras look at the same monitor, which shows a millisecond clock page. The two cameras view it from different angles, so the clock sits at a different position in each image, and the text is tilted in the image (in the images of the evening of 11/03 about −11° for the master and −10° for the slave, in the early morning of 11/04 about −11° and −15°; a negative sign means the text rises towards the right).
- The monitor refreshes at 60 Hz, so the smallest step the clock can show is one refresh period (16.7 ms).
- The on-screen clock shows UTC+8 time. The folder time stamps of the PCIe data are machine local time (UTC−8), so the same moment appears 16 hours later on the screen (for example folder time 11/03 22:14 corresponds to screen time 11/04 14:14); recognition converts accordingly before checking the readings.
- Δ = clock reading in the master image − clock reading in the slave image. A negative Δ means the master exposed earlier than the slave.
- How the readings were obtained and corrected is described in Section 3.5.

### 1.4 Groups and capture conditions

| Group | Resolution | Image size | Frames per group | Repeats | Saved sequence numbers | Completed (local time, UTC−8) |
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
- **Master–slave offset:** Section 3 shows that Δ changes from about −50 ms to about +26 ms between the two periods. The change coincides with the camera adjustment in time, but the mechanism has not been verified.
- **Failed capture attempts:** besides the formal data, the `groups` directory contains several incomplete capture attempts (Section 4.3). They are not used in the statistics.

## 2. Data Overview

"Captured" is the number of master/slave pairs actually saved. The reading of each pair comes from recognition (obs), a recognition reading corrected after checking the original image (chk), reading from the image (man) or a time-line estimate (est).

| Item | Value |
|---|---|
| Formal groups | 15 (G1 ×10 single-frame groups, G2 10 frames, G3 100 frames, G4 1 frame, G5 10 frames, G6 100 frames) |
| Formal frames per camera | 231 frames, 462 frames for both cameras |
| Captured pairs | 231 |
| Read by recognition (both cameras) | 177 |
| Corrected after checking the original image | 17 |
| Read from the image (single-frame groups) | 7 |
| Time-line estimate (at least one camera) | 30 |
| Used in the statistics (\|Δ\| ≤ 100 ms) | 231 |

### Source of the readings per group

| Group | Captured | obs | chk | man | est | Used |
|---|---|---|---|---|---|---|
| G1 (10 single-frame groups) | 10 | 3 | 0 | 7 | 0 | 10 |
| G2 | 10 | 8 | 1 | 0 | 1 | 10 |
| G3 | 100 | 91 | 1 | 0 | 8 | 100 |
| G4 | 1 | 1 | 0 | 0 | 0 | 1 |
| G5 | 10 | 5 | 2 | 0 | 3 | 10 |
| G6 | 100 | 69 | 13 | 0 | 18 | 100 |
| Total | 231 | 177 | 17 | 7 | 30 | 231 |

- All captured frames are present and no frames were lost (see Section 4). All 231 pairs have a reading and every master − slave Δ is within ±100 ms (−70 to +41 ms); no pair was excluded.
- The estimated frames are concentrated in G6 (18 pairs) and G3 (8 pairs); the reason is given in Section 3.5. Using only the pairs read by recognition, the medians of the early and the late period differ from those of all valid pairs by no more than 1 ms (Section 3.1), so the estimates do not change the conclusions.
- In the earlier version only 183 of the 231 pairs had a time reading and 169 were used in the statistics; with the readings completed, the sample size of every group equals the number of captured pairs.

## 3. Error Analysis

### 3.1 Results per group (Δ = master − slave, in ms)

"Captured pairs" is the number of master/slave pairs actually saved. "Valid pairs" is the number of pairs used in the statistics (including readings from the image, corrected values and estimates).

| Group | Captured pairs | Valid pairs | Median | Mean | Std. dev. | Robust σ (MAD) | 5%–95% | Min–Max |
|---|---|---|---|---|---|---|---|---|
| G1 | 10 | 10 | −50.0 | −50.1 | 0.3 | 0.0 | −51 to −50 | −51 to −50 |
| G2 | 10 | 10 | −50.0 | −49.7 | 5.4 | 0.7 | −55 to −42 | −58 to −36 |
| G3 | 100 | 100 | −50.5 | −52.7 | 6.1 | 3.0 | −68 to −43 | −70 to −39 |
| G4 | 1 | 1 | −50.0 | n/a | n/a | n/a | n/a | n/a |
| G5 | 10 | 10 | +32.5 | +29.5 | 8.8 | 3.7 | +14 to +38 | +11 to +39 |
| G6 | 100 | 100 | +24.5 | +24.7 | 9.0 | 12.6 | +12 to +36 | +5 to +41 |
| **Early G1–G4** | 121 | 121 | **−50.0** | −52.2 | 5.8 | **1.5** | −66 to −43 | −70 to −36 |
| **Late G5–G6** | 110 | 110 | **+26.0** | +25.1 | 9.0 | **11.9** | +11 to +37 | +5 to +41 |

Using only the pairs read by recognition (without readings from the image, corrected values and estimates) gives the following; the medians differ from the table above by no more than 1 ms:

| Period | Pairs | Median | Robust σ | Std. dev. |
|---|---|---|---|---|
| Early G1–G4 | 103 | −50.0 | 3.0 | 6.2 |
| Late G5–G6 | 74 | +25.0 | 13.3 | 10.1 |

Key points:
- The early period (11/03) has a median of −50 ms (−3 refresh periods) and the late period (11/04) a median of +26 ms. The signs are opposite and differ by about 76 ms.
- G1, G2, G4 and G5 have very few samples (1 to 10 pairs) and are for reference only. The conclusions rest mainly on the two 100-frame groups, G3 and G6.
- Compared with the earlier version of the recognition, the standard deviations are clearly smaller after the correction (early period from 21.9 to 5.8, G3 from 19.2 to 6.1, G6 from 28.2 to 9.0), so the large spread before was mainly reading error; the medians hardly changed (early period −50 ms unchanged, late period from +19 to +26 ms).
- The robust σ of the late period (11.9 ms) is larger than that of the early period (1.5 ms), mainly because of G6: there the position of the master relative to the screen clock moves by about 40 ms per frame, and Δ takes two or three neighbouring refresh-period values between +5 and +41 ms. This is quantization, not synchronization jitter.

### 3.2 In refresh periods (figure `fig_pcie_hist.png`)

| Periods | −4 | −3 | −2 | 0 | +1 | +2 |
|---|---|---|---|---|---|---|
| Early (121) | 10 | **106** | 5 | n/a | n/a | n/a |
| Late (110) | n/a | n/a | n/a | 2 | 50 | **58** |

- Early period: 106 of 121 (88%) are exactly at −3 periods (−50 ms); −4 to −2 together account for all 121.
- Late period: +1 and +2 periods together account for 108 of 110 (98%), with 58 pairs at +2 (+33 ms) and 50 at +1 (+17 ms).
- A single-frame Δ therefore falls almost entirely on integer multiples of 1 to 3 periods; the quantization is obvious. The true synchronization offset is better expressed as the median.

### 3.3 Drift within a group (figure `fig_pcie_drift.png`)

| Group | Pairs | Slope | Total over 100 frames |
|---|---|---|---|
| G3 | 100 | +0.082 ms/frame | +8.2 ms |
| G6 | 100 | +0.012 ms/frame | +1.2 ms |

The accumulated drift over 100 frames is only 1 to 8 ms, less than one refresh period (16.7 ms), so there is no noticeable relative drift between master and slave within one burst. The figures include estimated frames; an estimate comes from each camera's own time line and has little effect on the slope.

### 3.4 Error sources (approximate breakdown)

1. **Screen quantization:** the clock advances in 16.7 ms steps, so the quantization error of a single reading is about ±8 ms (uniform, σ ≈ 4.8 ms).
2. **Ghosting:** the old and new refresh are superimposed, giving a reading uncertainty of about ±1 to 2 periods (±17 to 33 ms); this is most obvious for frames close to a hundreds-of-milliseconds digit rollover (Section 3.5).
3. **Reading errors:** some may remain after the automatic correction, and the readings taken from the image of single-frame groups are also limited by ghosting.
4. **Camera and trigger jitter:** this cannot be separated out by the screen method; its upper bound is the total spread after the items above are combined.

The robust σ of the early period is about 1.5 ms, so the cameras' own jitter is no larger than one refresh period; the late period has a robust σ of about 11.9 ms, roughly one period.

### 3.5 Reading method and corrections

**(1) Tilt-corrected region recognition.** The clock text is tilted in the image of both cameras, and the earlier automatic localisation returned no time for some frames (48 pairs without a reading), while others were misread under ghosting. The clock region of each camera is now rotated to correct the tilt (one set of regions and angles for the evening of 11/03 and one for the early morning of 11/04) before recognition, merged with the earlier recognition results, and repaired with the time line of neighbouring frames. After this, all 231 pairs have a reading.

**(2) Why some readings are wrong.** At the moment the hundreds-of-milliseconds digit of the screen clock rolls over, the old and new digits overlap, and both the automatic recognition and the eye can misread them. The frame period here is about 540.5 ms, so the position of the master and of the slave relative to a 100 ms rollover moves by about 40.5 ms per frame; a frame falls a few milliseconds around a rollover with a probability of about one in six, and such frames are spread over the whole 100-frame group: 18 pairs in G6 and 8 pairs in G3 needed a time-line estimate. For example, for the master of G6 sequence 17 the clearer layer in the image is .384, which should be the digits of the two refresh periods .283 and .300 put together; on the time line of that camera the master time of this frame is about 14:02.292, Δ +23 ms, in line with the group median.

**(3) Time-line estimates.** For groups of 10 frames or more, a straight line of time against sequence number is fitted for each camera (in the short groups both cameras share one frame period). A reading that is more than 40 ms from the line, or missing, is replaced by the value on the line and marked `est` in the `master` and `slave` columns of `pcie_final_pairs.csv`. In 30 pairs at least one camera has an estimated value: 1 pair in G2, 8 in G3, 3 in G5 and 18 in G6. The accuracy of an estimate is about ±8 ms (limited by the screen resolution).

**(4) Readings from the image for single-frame groups.** G1 (10 single-frame groups) and G4 have no time line; their clock regions were read one by one from the original images. The recognition readings of G1_01, G1_05, G1_07 and G4 agree with the image and are kept; the table lists the 7 pairs whose reading changed, marked `man` in the `master` and `slave` columns (where digits overlap, the clearer layer was read; the uncertainty is about 1 to 2 periods):

| Group | Recognition (master/slave) | Read from the image (master/slave) | Δ of the image reading |
|---|---|---|---|
| G1_02 | .682 / .712 (−30) | .662 / .712 | −50 |
| G1_03 | .722 / .779 (−57) | .729 / .779 | −50 |
| G1_04 | .779 / .809 (−30) | .779 / .829 | −50 |
| G1_06 | 22.599 / 22.599 (0) | 22.549 / 22.599 | −50 |
| G1_08 | .229 / .239 (−10) | .229 / .279 | −50 |
| G1_09 | .999 / .998 (+1) | .929 / .979 | −50 |
| G1_10 | .099 / .120 (−21) | .089 / .140 | −51 |

The milliseconds of the master in G1_10 are at a digit rollover; a manual check puts them between .079 and .099, and the midpoint .089 is used, Δ ≈ −51 ms (range −61 to −41 ms).

**(5) Check against the original images and corrections (G2, G3, G5, G6).** The readings that were 16 ms or more away from the time line of their camera (17 readings) and the pairs with an estimate (30 pairs) were picked out first; in addition a few pairs per group were drawn at random from the rest (3 in G2, 12 in G3, 3 in G5, 12 in G6). These 75 pairs were checked one by one against the clock region of the original images. In 17 of them one camera was off by 17 ms or more (13 in G6, 2 in G5, 1 in G2, 1 in G3), mainly milliseconds tens digits misread under ghosting (3 and 1, 5 and 3, 8 and 5, and so on); these were corrected from the image (marked `chk`). For frames at a hundreds-of-milliseconds rollover the clearer layer in the image can be two layers of digits put together (see (2)); these frames are not changed to the reading in the image and keep the time-line estimate. All other checked readings agree with the image within ±8 ms.

**(6) Limitation of the readings.** About 150 pairs outside the check were not checked one by one; estimated frames, corrected frames and readings from the image are marked in the result table, and the statistics are given both for all valid frames and for the frames read by recognition only (Section 3.1).

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

- **Average period over the whole burst** (frame 2 to frame 101, 99 intervals): G3 master 540.6 ms, slave 540.6 ms; G6 master 540.5 ms, slave 540.4 ms.
- This average is computed from the sequence-number difference, to check that the sequence counter and the screen time agree. All four values are around 540.5 ms, so adjacent sequence numbers are indeed one period apart; there is no case of consecutive numbers with a two-period gap in between. Frame loss itself is judged by the continuous sequence numbers and complete files above.
- **Adjacent-frame intervals** (only intervals between two frames both read by recognition; estimated frames are not used):

| Group · camera | Intervals | Median | Robust σ | Within ±2 periods of median | Deviating by > 200 ms |
|---|---|---|---|---|---|
| G3 master | 83 | 540 ms | 8.9 | 100% | 0 |
| G3 slave | 83 | 539 ms | 8.9 | 100% | 0 |
| G6 master | 49 | 533 ms | 5.9 | 100% | 0 |
| G6 slave | 49 | 539 ms | 13.3 | 100% | 0 |

- In the earlier version only about 75% of the adjacent intervals of G6 were within ±2 periods, the rest being reading errors; after the readings were corrected all intervals used are within ±2 periods, none deviates by more than 200 ms, and none exceeds 900 ms.
- The median intervals of master and slave differ by 0 to 6 ms, so both cameras ran at the same cadence.

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

1. **Synchronization offset:** the early PCIe median is about −50 ms (−3 refresh periods) with a robust σ of about 1.5 ms; the late median is about +26 ms with a robust σ of about 12 ms. The two periods have opposite signs, and the camera position was adjusted in between.
2. **Within a burst:** drift over 100 frames is only 1 to 8 ms; there is no noticeable relative drift.
3. **Frame loss:** in the 15 formal groups (462 frames) the sequence numbers are continuous, the files are complete and there are no duplicate frames, so no frames were lost. The frame period is about 540.5 ms and identical for master and slave.
4. **Anomalous points:** the pairs with |Δ| > 100 ms that were excluded before were reading problems: recognition failures caused by the tilted text, and overlapping digits close to a hundreds-of-milliseconds rollover. After the readings were corrected all 231 pairs are within ±100 ms (−70 to +41 ms); the readings were also checked against the original images on a sample and corrected where they were off (Section 3.5).
5. **Measurement accuracy:** the smallest step of the screen method is 16.7 ms and the reading error of a single frame is about ±1 to 2 periods. A single-frame Δ should therefore not be treated as an exact value; the median is more reliable.

## 6. Limitations

- G1, G2, G4 and G5 have very few samples and are for reference only. The readings of single-frame groups come from the images and have an uncertainty of about 1 to 2 periods where digits overlap.
- In 30 pairs the time of one camera is a time-line estimate (most in G6, 18 pairs). An estimate has an accuracy of about ±8 ms and is not a measured reading; using only the pairs read by recognition changes the medians by no more than 1 ms.
- Of the recognition readings only 75 pairs were checked against the original images and about 150 were not, so a few deviations of about 20 ms probably remain, which should not affect the medians.
- The sign difference between the early and late periods is currently known only to coincide in time with the camera repositioning; the mechanism has not been verified.
