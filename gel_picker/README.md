# Gel Band Picker

Interactive browser-based tool for quantifying Coomassie-stained SDS-PAGE gel images.  
Draw rectangles directly on your gel, set per-lane loading controls, inspect area curves, and export CSVs.

---

## Screenshot

```
┌──────────────────────────────────┬──────────────────────────┐
│                                  │  Lane profiles / area    │
│        Gel image                 │  curve panel (live)      │
│   [drag boxes on bands]          │                          │
│                                  │  Lane table + SNR/sat.   │
├──────────────────────────────────┴──────────────────────────┤
│  Mode  │  Lock size  │  BG radius  │  Quantify  │  Save     │
└────────────────────────────────────────────────────────────┘
```
![../images/interface_band.png]

---

## Features

### Gel picker
| Feature | Details |
|---|---|
| **Rectangle selection** | Click-drag to draw band boxes and normalization boxes directly on the gel image |
| **Move & resize** | Switch to Move mode, drag any box to reposition, drag corner handle to resize |
| **Locked box size** | Draw one reference box, lock its size — every subsequent click stamps the exact same dimensions |
| **Per-lane norm boxes** | Each lane can have its own independent loading-control region (handles migration differences) |
| **Norm all lanes** | Or draw one norm box and apply it to every lane at once |

### Signal inspection
| Feature | Details |
|---|---|
| **Live profile panel** | Background-subtracted intensity profile updates as you draw/move boxes |
| **Area curve view** | Toggle to see the filled area under the curve — exactly what is being integrated |
| **Background overlay** | Dashed red line shows the rolling-ball background estimate so you can verify it isn't eating into the signal |
| **Lane isolation** | Click any lane pill to inspect a single lane in detail |
| **Adjustable BG radius** | Slider controls rolling-ball radius; profile updates live so you can tune it |

### Quantification & normalisation
| Feature | Details |
|---|---|
| **Gaussian peak fitting** | Accurate area integration with trapezoid fallback |
| **Rolling-ball background subtraction** | 1D morphological background removal per lane |
| **Two normalisation modes** | ÷ total lane intensity **or** ÷ loading control (norm box) |
| **Fold-change vs Lane 1** | Computed for both normalisation strategies |
| **SNR per band** | Peak signal / background noise; colour-coded green/amber/red |
| **Saturation detection** | Flags bands where ≥ 2 % of pixels are clipped — these cannot be accurately quantified |

### Results page
- Bar chart and line plot with fold-change annotations
- Dual y-axis (signal + fold-change)
- Editable axis labels for direct copy-paste into publications
- Saturated bands highlighted in red
- One-click PNG download for each chart

### Session & reproducibility
- **Analyst name**, **experiment**, and **notes** fields stamped into every CSV row and figure footer
- **Live UTC clock** displayed at all times
- Timestamp in ISO format in CSV output
- Rolling-ball radius saved per row so analysis is fully reproducible

---

## Quick start

### Option A — Conda (recommended)

```bash
# 1. Clone
git clone https://github.com/andreamariossi/analysis-tools/gel-picker.git
git clone https://github.com/andreamariossi/analysis-tools/tree/main/gel_picker/gel-picker.git
cd gel-picker

# 2. Create environment
conda env create -f environment.yml
conda activate gel-picker

# 3. Run
python gel_picker.py path/to/gel1.tif path/to/gel2.tif
```

Then open **http://localhost:5050** in your browser.

### Option B — pip

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/gel-picker.git
cd gel-picker

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run
python gel_picker.py path/to/gel1.tif path/to/gel2.tif
```

### Option C — pip with virtual environment (cleanest)

```bash
git clone https://github.com/YOUR_USERNAME/gel-picker.git
cd gel-picker

python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

pip install -r requirements.txt

python gel_picker.py gel1.tif gel2.tif
```

---

## Supported image formats

`.tif` / `.tiff`, `.png`, `.jpg` / `.jpeg`, `.bmp`

Both 8-bit and 16-bit grayscale images are supported.  
RGB images are automatically converted to grayscale by averaging channels.

---

## Workflow walkthrough

### 1. Session info
Fill in **Analyst**, **Experiment**, and **Notes** in the bar at the top.  
These are saved in every CSV row and printed on every exported figure.

### 2. Draw band boxes
Make sure **🔵 Band box** mode is selected (default).  
Click and drag a rectangle tightly around the band you want to quantify.  
Repeat for each lane left to right.

> **Tip — locked size:** After drawing your first band box, tick **Lock Band** in the toolbar.  
> Every subsequent drag will snap to the same width × height, keeping all boxes identical.

### 3. Draw normalization boxes
Switch to **🟢 Norm (lane)** and drag a rectangle around the loading control band for the most recently drawn lane.  
Repeat for each lane.

> **Or** use **🟢 Norm (all)** to draw a single box applied to every lane simultaneously.  
> Use per-lane norm boxes when bands migrate slightly differently between lanes.

### 4. Inspect the profiles
The **right-hand panel** shows live background-subtracted profiles.

- **Signal** tab — profile line + dashed background
- **Area curve** tab — filled area under the curve, labelled with the integrated value
- **Both** tab — signal line + filled area together

Click a **lane pill** at the bottom to isolate one lane.  
Adjust the **BG radius** slider until the dashed background hugs the baseline without creeping into the band.

> ⚠ If the filled area extends significantly beyond the visible band edges, your band box is too wide or the BG radius is too large.

### 5. Check quality indicators
The lane table shows:

| Column | Meaning |
|---|---|
| **SNR** | Signal-to-noise ratio. Green ≥ 10, amber 4–10, red < 4. Avoid publishing red-SNR bands. |
| **⚠** | Saturation warning. ⚠ means > 2 % of pixels in the band box are at maximum intensity — the band is non-linear and cannot be accurately quantified. Re-run at lower protein load. |

### 6. Move or resize boxes
Switch to **🟡 Move / resize** mode.  
Click any box to select it (turns gold).  
- **Drag the body** to reposition
- **Drag the bottom-right corner handle** to resize

### 7. Quantify
Click **Quantify ▶**.  
Results are computed and the status bar confirms how many lanes were quantified.  
Any saturation warnings are surfaced here.

### 8. View results
Click the **📊 Results** tab.

- Choose normalisation strategy (total lane / loading control)
- Toggle fold-change annotations and the line plot
- Edit axis labels directly — they update the charts in real time
- Click **⬇ Download** on either chart to save a PNG

### 9. Save CSV
Click **Save CSV**.  
The file is saved next to your original gel image with `_quantification.csv` appended.

---

## CSV output columns

| Column | Description |
|---|---|
| `lane` | Lane number (1-indexed, left to right) |
| `timestamp` | ISO datetime when Quantify was run |
| `analyst` | Name entered in session bar |
| `notes` | Experiment + notes fields |
| `band_x0/y0/x1/y1` | Band box pixel coordinates |
| `rb_radius` | Rolling-ball radius used |
| `area_raw` | Background-subtracted integrated area (arbitrary units) |
| `snr` | Signal-to-noise ratio |
| `saturation_frac` | Fraction of pixels at max intensity |
| `saturation_warn` | True if saturation_frac > 0.02 |
| `lane_total_int` | Total background-subtracted intensity of entire lane column |
| `norm_area` | Integrated area of the normalization box |
| `area_norm_total` | `area_raw ÷ lane_total_int` |
| `area_norm_loadctrl` | `area_raw ÷ norm_area` |
| `fc_vs_L1_total` | Fold-change vs Lane 1 (total normalisation) |
| `fc_vs_L1_loadctrl` | Fold-change vs Lane 1 (loading control normalisation) |

---

## Methods text (for papers)

> Gel images were quantified using Gel Band Picker (v5, [GitHub URL]).  
> Band regions and per-lane loading controls were defined by manually drawn rectangles.  
> Background was estimated by 1D rolling-ball morphological opening with a radius of [N] pixels and subtracted from each lane profile.  
> Band area was computed by trapezoidal integration of the background-subtracted signal within the selected region.  
> Values were normalised to [total lane intensity / loading control band area] and expressed as fold-change relative to Lane 1.  
> Bands with > 2% pixel saturation were excluded from quantification.

---

## Notes on accuracy

**Why per-lane norm boxes?**  
Even small run-to-run differences in gel migration mean a single horizontal norm box position will not land at the same row in every lane. Drawing individual norm boxes per lane ensures the normalization region always captures the same band in each lane.

**Why rolling-ball background?**  
Gel backgrounds are rarely flat — they have broad intensity gradients from uneven staining or destaining. Rolling-ball background subtraction models this locally without requiring manual baseline points.

**What rolling-ball radius should I use?**  
A good starting point is 2–3× the height of your tallest band. If the dashed background line in the profile panel dips into the band peak, increase the radius. If it fails to follow slow background drift, decrease it.

**Saturation is non-negotiable**  
Saturated pixels cannot encode quantitative information. If you see a saturation warning (⚠), the only valid remedy is to reload and re-image at a lower protein concentration or shorter exposure.

---

## Requirements

- Python 3.10 – 3.12
- numpy ≥ 1.24
- scipy ≥ 1.10
- pandas ≥ 1.5
- matplotlib ≥ 3.7
- Pillow ≥ 9.0
- scikit-image ≥ 0.20
- flask ≥ 2.3

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

## Contributing

Bug reports, feature requests, and pull requests are welcome.  
Please open an issue before starting a large change so we can discuss the approach.

---

```
Gel Band Picker (2025). Interactive browser-based SDS-PAGE gel quantification tool.
GitHub: https://github.com/YOUR_USERNAME/gel-picker
```
