# Damage model reference: shaking → intensity → building damage

Every formula here is implemented in `damage_model.py`. Units: km, cm/s.

## 1. Ground motion: Si & Midorikawa (1999)

Peak ground velocity on engineering bedrock (Vs = 600 m/s):

```
log10 PGV600 = 0.58·Mw + 0.0038·D + d − 1.29 − log10(X + 0.0028·10^(0.50·Mw)) − 0.002·X
```

- `Mw` moment magnitude. For shallow crustal events given in JMA magnitude: `Mw = 0.78·Mj + 1.08`.
- `D` focal depth (km). `X` shortest distance to the fault rupture plane (km). For a point source, use hypocentral distance.
- `d` = 0 crustal, −0.02 interplate, +0.12 intraplate.
- `0.0028·10^(0.5·Mw)` is the near-source saturation term; no extra distance cap is needed.
- PGV is the larger horizontal component. Aleatory scatter is about σ ≈ 0.23 in log10 units in the original paper (inferred, not checked against the source). Add `N(0, σ)` per scenario if you want shaking variability.

Source: Si, H. & Midorikawa, S. (1999), *J. Struct. Constr. Eng. (AIJ)* 523, 63–70. Constants were checked against the national hazard map report (Headquarters for Earthquake Research Promotion 2004, probabilistic seismic hazard map methodology, eq. 2.2.3-1, p. 98).

## 2. Soil amplification

**Preferred: J-SHIS 250 m grid of surface-soil amplification factors (ARV).** J-SHIS ARV is relative to Vs = 400 m/s, so:

```
PGV_surface = PGV600 × 1.31 × ARV_jshis
```

(1.31 is the J-SHIS 600 → 400 m/s factor, HERP 2004, p. 98.) Download the "surface ground" (shallow soil) layer for map area 5339 from J-SHIS. It is blocked from our sandbox, so download it on a laptop. Central Shinjuku's Yodobashi plateau is mostly ARV ~1.2–1.6. The Kanda River valley and filled valleys (e.g. Okubo, Takadanobaba lowlands) run higher. These ranges are from general knowledge; check them against the mesh.

**Fallback, from AVS30** (Matsuoka & Midorikawa 1994, valid 100 < AVS30 < 1500 m/s, relative to Vs 600):

```
log10 ARV = 1.83 − 0.66·log10(AVS30)
PGV_surface = PGV600 × ARV
```

## 3. JMA seismic intensity (shindo): Midorikawa et al. (1999)

```
I = 2.68 + 1.72·log10(PGV_surface)        valid 4 ≤ I ≤ 7
```

| I | < 4.5 | 4.5–5.0 | 5.0–5.5 | 5.5–6.0 | 6.0–6.5 | ≥ 6.5 |
|---|---|---|---|---|---|---|
| shindo | ≤ 4 | 5− | 5+ | 6− | 6+ | 7 |

## 4. Fragility curves: Murao & Yamazaki (2000)

These are lognormal in PGV and fitted to building-by-building damage certificates from the 1995 Kobe earthquake:

```
P(damage ≥ state | PGV) = Φ( (ln PGV − λ) / ζ )      PGV in cm/s at the surface
```

"Collapse" = total collapse (Japan's official "complete damage" category). "Collapse + half" = complete or partial damage, so P(partial only) = P(collapse + half) − P(collapse).

| Structure | Era (our bucket ← source bin) | Collapse λ | ζ | Collapse+half λ | ζ |
|---|---|---|---|---|---|
| Wood | pre-1971 ← 1962–71 | 4.45 | 0.342 | 4.02 | 0.456 |
| Wood | 1971–81 ← 1972–81 | 4.73 | 0.378 | 4.25 | 0.395 |
| Wood | post-1981 ← 1982–94 | 5.12 | 0.496 | 4.61 | 0.474 |
| RC | pre-1971 | 5.12 | 0.646 | 4.72 | 0.691 |
| RC | 1972–81 | 5.33 | 0.575 | 4.85 | 0.612 |
| RC | 1982–94 | 6.00 | 0.789 | 5.33 | 0.789 |
| Steel | pre-1971 | 4.64 | 0.619 | 4.25 | 0.712 |
| Steel | 1972–81 | 4.97 | 0.490 | 4.49 | 0.549 |
| Steel | 1982–94 | 5.64 | 0.731 | 5.01 | 0.733 |
| Light steel | pre-1971 | 4.70 | 0.550 | 4.41 | 0.504 |
| Light steel | 1972–81 | 5.82 | 0.972 | 4.95 | 0.855 |
| Light steel | 1982–94 | 6.19 | 1.101 | 5.28 | 0.865 |

Older wood bins, if you ever get real years: −1951 (4.36, 0.411 | 3.66, 0.674) and 1952–61 (4.44, 0.353 | 3.97, 0.490). SRC uses the RC row.

Source: Murao, O. & Yamazaki, F. (2000), "Development of fragility curves for buildings based on damage survey data of a local government after the 1995 Hyogoken-Nanbu earthquake", *J. Struct. Constr. Eng. (AIJ)* 527, 189–196. The values were read from the tables reproduced in GIROJ (General Insurance Rating Organization of Japan) Earthquake Insurance Research No. 37, ch. III, pp. 40 and 50.

**Caveats:**
- The Kobe curves run high. Post-2000 buildings, especially wood after the 2000 code revision, perform better than the 1982–94 bin (Kumamoto 2016). Tokyo's 2022 estimate splits post-1981 wood into 1981–90, 1991–2000 and 2001+ for that reason.
- The official Tokyo and Cabinet Office models use curves in JMA intensity, not PGV. Their parameters are published only as charts (Cabinet Office 2013 Tokyo inland earthquake method, p. 5; TMG 2022 ch. 6, pp. 6-5 to 6-7), so we couldn't extract exact constants. If your counts come out above Tokyo's estimate, shift λ up rather than changing the shaking.
- Worked example (`python3 damage_model.py`): Mw 7.3, X = 20 km, ARV 1.8 gives PGV 76 cm/s, I = 5.9 (6−). Pre-1971 wood collapse ≈ 36%; post-1981 RC ≈ 1.7%.

## 5. Validation target

Tokyo Metropolitan Government (2022), "Damage Assumptions for Tokyo from a Tokyo Inland Earthquake". Scenario: earthquake directly beneath southern central Tokyo (Mw 7.3). Ward-level collapse counts for Shinjuku-ku are in the report's ward tables. Our box is about 1/8 of the ward's area, so scale by building count, not area. TMG's own inputs: 250 m intensity mesh, six wood eras and three non-wood eras, soft or hard ground split at 0.5 s predominant period (TMG 2022 ch. 6, pp. 6-3 to 6-5).

## 6. Fire (stretch): Cabinet Office (2013) Tokyo inland earthquake method, p. 10

- Initial-extinguishing success: 67% at shindo ≤ 6−, 30% at 6+, 15% at 7. Burning fires = (1 − success) × ignitions.
- Ignition from collapsed buildings: 0.0449% (winter, late night), 0.0629% (summer, noon), 0.153% (winter, 18:00) per collapsed building.
- Non-collapse ignition per building, winter 18:00 (worst case):

| Use | 5− | 5+ | 6− | 6+ | 7 |
|---|---|---|---|---|---|
| Restaurant | 0.0047% | 0.0157% | 0.0541% | 0.1657% | 0.509% |
| Retail | 0.0007% | 0.0022% | 0.0085% | 0.0302% | 0.158% |
| Office / other | 0.0003% | 0.0012% | 0.0052% | 0.0216% | 0.177% |
| House / apartment | 0.0010% | 0.0034% | 0.0109% | 0.0351% | 0.115% |

- Spread: Tokyo Fire Department 2001 spread-speed model, 250 m mesh, 3 or 8 m/s wind. Roads, rails and rivers act as firebreaks. Use `fireproof_type` in the buildings file: `fire_resistant` buildings stop spread, and wood ones carry it.
