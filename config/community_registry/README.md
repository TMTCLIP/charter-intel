# Community Registry

Each state that uses NCES-based discovery gets one YAML file here:

    config/community_registry/{state_lower}.yaml

Example: `config/community_registry/ms.yaml`, `config/community_registry/tn.yaml`

States without a registry file (e.g. NM) continue using roster-based discovery via
`data/raw/{state}/charter_roster.csv`.

---

## Schema

Each top-level key is a `community_id` slug.

```yaml
{community_id}:               # slug: {state_lower}-{city_lower_hyphenated}
  display_name: str           # city name shown to users, e.g. "Jackson"
  state: str                  # 2-letter abbreviation, e.g. "MS"
  district_nces_id: str       # 7-digit NCES LEA ID, e.g. "2801230"
  district_name: str          # full NCES district name
  has_charters: bool          # true if charter roster lists any school in this district
  enrollment: int             # total district enrollment from CCD
  source: str                 # "ccd_auto" or "roster_manual"
```

### community_id slug format

```
{state_lower}-{city_lower_hyphenated}
```

- Spaces replaced with hyphens
- Non-alphanumeric characters removed (Unicode normalized to ASCII first)
- All lowercase

**Examples:** `ms-jackson`, `ms-port-gibson`, `tn-nashville`, `wi-milwaukee`

**Disambiguation:** if two districts in the same state share a city name, the
7-digit NCES LEA ID is appended as a suffix:

```
ms-springfield-2801001
ms-springfield-2801002
```

---

## Enrollment floor

The build script applies a minimum enrollment filter before writing the registry.

- **Default:** 500 students
- Override with `--floor N` at build time
- Districts below the floor are excluded and logged

---

## How to run the build script

```bash
python3 scripts/build_community_registry.py \
    --state MS \
    --ccd-file data/raw/ms/nces_lea_dir_2024.csv \
    --roster data/raw/ms/charter_roster.csv \   # optional: sets has_charters
    --floor 500                                 # optional: default 500
```

Use `--dry-run` to preview output without writing the file:

```bash
python3 scripts/build_community_registry.py \
    --state MS \
    --ccd-file data/raw/ms/nces_lea_dir_2024.csv \
    --dry-run
```

Use `--output` to write to a non-default path:

```bash
python3 scripts/build_community_registry.py \
    --state MS \
    --ccd-file data/raw/ms/nces_lea_dir_2024.csv \
    --output /tmp/ms_preview.yaml
```

---

## Adding or overriding entries manually

Set `source: roster_manual` on any entry you add or override by hand so it is
distinguishable from auto-generated entries:

```yaml
ms-port-gibson:
  display_name: "Port Gibson"
  state: MS
  district_nces_id: "2803150"
  district_name: "Claiborne County School District"
  has_charters: false
  enrollment: 1200
  source: roster_manual
```

Manually added entries survive future rebuilds only if they are re-added after
re-running the build script (the script overwrites the file). Keep manual
entries documented here or in a separate override file if they are permanent.
