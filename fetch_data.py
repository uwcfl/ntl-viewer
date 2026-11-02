"""
Fetches and processes North Temperate Lakes LTER data from the EDI data
repository (PASTA REST API) for the NTL-LTER Data Viewer.

The original physical/chemical/zooplankton/catch logic mirrors the R/Shiny
app's server.R (loadLTERnutrients / loadLTERions / loadLTERtemp /
loadLTERsecchi / loadLTERice / loadLTERzoop + the pivoting steps that build
`allLTER`). Additional core datasets (chlorophyll, light extinction, snow &
ice, lake levels, Kegonsa/Waubesa, phytoplankton, macroinvertebrates,
macrophytes, fish lengths) were added on top of that.

Run weekly by .github/workflows/update-data.yml (or by hand). Writes:
  data/vars/<var>.json     - one file per variable (see "kind" below)
  data/matchtable.json     - variable registry: code, display name, group,
                             kind, units, source packages, citation URLs
  data/lakelocations.json  - lake metadata + coordinates (from package 434
                             where available)
  data/qc/qc_log.csv       - values removed or flagged by the plausibility
                             checks in QC_RULES (for the data manager)
  data/last_download.json  - EDI revisions used for this build

Variable kinds (the viewer picks controls and plots from this):
  standard  - lakeid, lakename, year4, daynum, sampledate, depth, rep, value
              (depth may be a label such as "0-2" for integrated samples)
  annual    - same columns as standard, one value per lake-year
              (sampledate = Jan 1 of year4); time-series only
  catch     - fish / crayfish CPUE: spname, gearid, total_caught, effort, CPUE
  taxon     - one value per lake-year-taxon: taxon, [gear], value, [lo, hi, n]
  lengthfreq- fish length frequency: taxon, year4, bin, n
"""

import csv
import io
import json
import math
import os
import re
import sys
from datetime import datetime, timezone

import pandas as pd
import requests

PASTA_BASE = "https://pasta.lternet.edu/package"
SCOPE = "knb-lter-ntl"
HEADERS = {"User-Agent": "ntl-lter-viewer/1.0 (weekly data refresh)"}

# Packages monitored for updates (any new revision triggers a rebuild)
PACKAGES = [
    "1", "2", "3", "6", "7", "8", "11", "14", "21", "23", "24", "25", "28",
    "29", "30", "31", "32", "33", "34", "35", "37", "38", "88", "90", "245",
    "259", "376", "401", "434",
]
TRACKING_FILE = "data/last_download.json"
QC_FILE = "data/qc/qc_log.csv"

# EDI now requires an access key on every PASTA request (?key=<access_key>).
# Read it from the environment so the actual value never lives in source
# control; the GitHub Actions workflow injects it from a repo secret.
EDI_ACCESS_KEY = os.environ.get("EDI_ACCESS_KEY")
if not EDI_ACCESS_KEY:
    raise RuntimeError(
        "EDI_ACCESS_KEY environment variable is not set. "
        "Set it as a GitHub Actions secret and pass it in via `env:` "
        "in update-data.yml."
    )


def edi_url(identifier) -> str:
    return f"https://portal.edirepository.org/nis/mapbrowse?scope={SCOPE}&identifier={identifier}"


# ---------------------------------------------------------------------------
# Generic EDI helpers
# ---------------------------------------------------------------------------

REVISION_CACHE = {}


def _get(path: str, timeout: int = 60, **params) -> requests.Response:
    params["key"] = EDI_ACCESS_KEY
    r = requests.get(f"{PASTA_BASE}/{path}", headers=HEADERS, params=params, timeout=timeout)
    r.raise_for_status()
    return r


def newest_revision(identifier: str) -> str:
    if identifier not in REVISION_CACHE:
        REVISION_CACHE[identifier] = _get(f"eml/{SCOPE}/{identifier}", filter="newest").text.strip()
    return REVISION_CACHE[identifier]


def check_for_updates() -> tuple[bool, dict[str, str]]:
    """Check EDI for newest revisions and compare against saved tracking file."""
    print("Checking EDI repository for dataset revisions...")
    current_revisions = {}
    for pkg in PACKAGES:
        try:
            current_revisions[pkg] = newest_revision(pkg)
        except Exception as exc:  # noqa: BLE001
            print(f"  !! failed to check revision for package {pkg}: {exc}", file=sys.stderr)

    if os.environ.get("FORCE_REBUILD"):
        print("FORCE_REBUILD set; processing regardless of revisions.")
        return True, current_revisions

    if not os.path.exists(TRACKING_FILE):
        print("No prior download tracking file found. Processing required.")
        return True, current_revisions

    try:
        with open(TRACKING_FILE, "r") as f:
            saved_state = json.load(f)
        saved_revisions = saved_state.get("revisions", {})
        last_date = saved_state.get("last_download_date", "unknown")

        if current_revisions and saved_revisions == current_revisions:
            print(f"No dataset updates detected on EDI since last download on {last_date}.")
            return False, current_revisions

        print("New dataset updates detected on EDI.")
        return True, current_revisions
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: Failed to read tracking file ({exc}). Proceeding with download.")
        return True, current_revisions


def entity_list(identifier: str, revision: str) -> list[tuple[str, str]]:
    """[(entity_id, entity_name), ...] in package order."""
    text = _get(f"name/eml/{SCOPE}/{identifier}/{revision}").text
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        eid, _, name = line.partition(",")
        out.append((eid.strip(), name.strip()))
    if not out:
        raise RuntimeError(f"No data entities found for {SCOPE}.{identifier}.{revision}")
    return out


def read_entity_csv(identifier: str, revision: str, entity_id: str) -> pd.DataFrame:
    r = _get(f"data/eml/{SCOPE}/{identifier}/{revision}/{entity_id}", timeout=600)
    return pd.read_csv(io.StringIO(r.text), low_memory=False)


def load_latest_package(identifier: str, entity=0) -> pd.DataFrame:
    """
    Fetch one data entity from the newest revision of a package.
    `entity` is either an index (0 = first entity) or a case-insensitive
    substring of the entity name (e.g. "Taxonomic Codes").
    """
    revision = newest_revision(identifier)
    entities = entity_list(identifier, revision)
    if isinstance(entity, int):
        eid, name = entities[entity]
    else:
        matches = [(e, n) for e, n in entities if entity.lower() in n.lower()]
        if not matches:
            raise RuntimeError(
                f"No entity matching {entity!r} in {SCOPE}.{identifier}.{revision}: "
                f"{[n for _, n in entities]}"
            )
        eid, name = matches[0]
    df = read_entity_csv(identifier, revision, eid)
    print(f"  loaded {SCOPE}.{identifier}.{revision} '{name}' -> {df.shape}")
    return df


# ---------------------------------------------------------------------------
# Quality control
#
# Rules are applied to the long-format "standard" data after all loaders run
# (and inside the fish / zooplankton loaders for lengths). "remove" drops the
# value from the viewer; "review" keeps it but logs it for the data manager.
# Every hit is written to data/qc/qc_log.csv.
# ---------------------------------------------------------------------------

QC_RULES = {
    # var: [(min, max, action, reason)]
    "wtemp": [(-1, 40, "remove", "water temperature outside -1 to 40 degC")],
    "o2": [(0, 25, "review", "dissolved oxygen above 25 mg/L")],
    "o2sat": [(0, 300, "review", "oxygen saturation above 300%")],
    "ph": [(2, 12, "remove", "pH outside 2 to 12")],
    "phair": [(2, 12, "remove", "pH outside 2 to 12")],
    "chlor": [(0, 300, "review", "chlorophyll above 300 ug/L")],
}
FISH_LENGTH_MAX_MM = 1500   # largest fish in these lakes (muskellunge) ~1.4 m
ZOOP_LENGTH_MAX_MM = 20     # largest crustacean zooplankton incl. spines

QC_LOG: list[dict] = []
QC_FIELDS = ["package", "variable", "lakeid", "sampledate", "year4", "depth",
             "taxon", "value", "action", "reason"]


def qc_log_rows(df: pd.DataFrame, package, variable, action, reason, value_col="value", taxon_col=None):
    for _, r in df.iterrows():
        QC_LOG.append({
            "package": package,
            "variable": variable,
            "lakeid": r.get("lakeid"),
            "sampledate": r.get("sampledate"),
            "year4": r.get("year4"),
            "depth": r.get("depth"),
            "taxon": r.get(taxon_col) if taxon_col else None,
            "value": r.get(value_col),
            "action": action,
            "reason": reason,
        })


def apply_qc_rules(df: pd.DataFrame) -> pd.DataFrame:
    """Apply QC_RULES to long-format standard data (needs item, value, source)."""
    keep = pd.Series(True, index=df.index)
    for var, rules in QC_RULES.items():
        is_var = df["item"] == var
        for lo, hi, action, reason in rules:
            bad = is_var & ((df["value"] < lo) | (df["value"] > hi))
            if not bad.any():
                continue
            hits = df[bad]
            for pkg, sub in hits.groupby("source"):
                qc_log_rows(sub, pkg, var, action, reason)
            print(f"  QC {var}: {int(bad.sum())} value(s) -> {action} ({reason})")
            if action == "remove":
                keep &= ~bad
    return df[keep]


def write_qc_log():
    os.makedirs(os.path.dirname(QC_FILE), exist_ok=True)
    with open(QC_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=QC_FIELDS)
        w.writeheader()
        for row in QC_LOG:
            w.writerow({k: ("" if v is None or (isinstance(v, float) and math.isnan(v)) else v)
                        for k, v in row.items()})
    print(f"Wrote {QC_FILE} ({len(QC_LOG)} rows)")


# ---------------------------------------------------------------------------
# Generic long-pivot helper (mirrors the repeated
# rename(value_/error_) -> pivot_longer -> flag-filter -> drop error pattern
# used throughout server.R)
# ---------------------------------------------------------------------------

ID_CANDIDATES = ["lakeid", "year4", "daynum", "sampledate", "depth", "rep", "sta", "event"]
BAD_FLAG_PATTERN = re.compile(r"[AKLHU]")  # flags to exclude, matches str_detect(error,'A|K|L|H[|U]')


def melt_value_flag_columns(df: pd.DataFrame, id_cols: list[str], exclude_bad_flags: bool = True) -> pd.DataFrame:
    """
    Generic version of the R pattern:
      pivot_longer(values) -> filter(!is.na(value) & value >= 0)
      -> filter(!str_detect(error, 'A|K|L|H|U') | is.na(error))

    Any column literally named "flag<x>" is paired with the value column "<x>".
    Columns with no matching flag column are still melted (error = NA).
    """
    id_cols = [c for c in id_cols if c in df.columns]
    flag_cols = {c for c in df.columns if c.startswith("flag")}
    value_cols = [c for c in df.columns if c not in id_cols and c not in flag_cols]

    records = []
    for item in value_cols:
        sub = df[id_cols + [item]].copy().rename(columns={item: "value"})
        flag_col = f"flag{item}"
        sub["error"] = df[flag_col] if flag_col in df.columns else pd.NA
        sub["item"] = item
        records.append(sub)

    if not records:
        return pd.DataFrame(columns=id_cols + ["item", "value"])

    out = pd.concat(records, ignore_index=True)
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    out = out[out["value"].notna() & (out["value"] >= 0)]

    if exclude_bad_flags:
        bad = out["error"].astype("string").fillna("").apply(lambda s: bool(BAD_FLAG_PATTERN.search(s)))
        out = out[~bad]

    return out.drop(columns=["error"])


def clean_negatives(df: pd.DataFrame, package=None) -> pd.DataFrame:
    """
    mutate(across(everything(), ~replace(., . < 0, NA)))
    Missing-value sentinels (<= -99) are logged to the QC file first.
    """
    numeric_cols = df.select_dtypes(include="number").columns
    df = df.copy()
    for c in numeric_cols:
        if package is not None and c not in ("year4", "daynum", "depth", "rep", "sta"):
            sentinel = df[c] <= -99
            if sentinel.any():
                qc_log_rows(df[sentinel], package, c, "remove",
                            "negative missing-value code in data column", value_col=c)
        df.loc[df[c] < 0, c] = pd.NA
    return df


def to_standard(df: pd.DataFrame, item: str, value_col: str, date_col="sampledate",
                depth=0, rep=1, source=None) -> pd.DataFrame:
    """Build standard long-format rows from a simple one-value-per-row table."""
    out = pd.DataFrame({
        "lakeid": df["lakeid"].astype(str).str.upper().str.strip(),
        "sampledate": pd.to_datetime(df[date_col], errors="coerce"),
        "value": pd.to_numeric(df[value_col], errors="coerce"),
    })
    out["year4"] = out["sampledate"].dt.year
    out["daynum"] = out["sampledate"].dt.dayofyear
    out["depth"] = df["depth"].values if (isinstance(depth, str) and depth in df.columns) else depth
    out["rep"] = df["rep"].values if (isinstance(rep, str) and rep in df.columns) else rep
    out["item"] = item
    out["source"] = source
    out = out[out["value"].notna() & (out["value"] >= 0) & out["sampledate"].notna()]
    return out


def annual_rows(df: pd.DataFrame, item: str, year_col: str, value_col: str, source=None) -> pd.DataFrame:
    """One value per lake-year, stored with sampledate = Jan 1 (as iceduration always has been)."""
    out = pd.DataFrame({
        "lakeid": df["lakeid"].astype(str).str.upper().str.strip(),
        "year4": pd.to_numeric(df[year_col], errors="coerce"),
        "value": pd.to_numeric(df[value_col], errors="coerce"),
    })
    out = out[out["year4"].notna() & out["value"].notna()].copy()
    out["year4"] = out["year4"].astype(int)
    out["sampledate"] = pd.to_datetime(out["year4"].astype(str) + "-01-01")
    out["daynum"] = 1
    out["depth"] = 0
    out["rep"] = 1
    out["item"] = item
    out["source"] = source
    return out


# ---------------------------------------------------------------------------
# Standard-format loaders: original viewer datasets
# ---------------------------------------------------------------------------


def split_wslh(raw: pd.DataFrame) -> pd.DataFrame:
    """
    State Lab of Hygiene columns (<var>_WSLH in package 1, <var>_wslh in 401)
    are reported in mg/L; rename them to <var>.WSLH so the melt keeps the
    suffix, then convert_wslh() scales them to ug/L and drops the suffix.
    """
    ren = {}
    for c in raw.columns:
        m = re.match(r"^(flag)?(.+)_wslh$", c, flags=re.I)
        if m:
            ren[c] = f"{m.group(1) or ''}{m.group(2)}.WSLH"
    return raw.rename(columns=ren)


def convert_wslh(out: pd.DataFrame) -> pd.DataFrame:
    is_wslh = out["item"].str.endswith(".WSLH")
    out.loc[is_wslh, "value"] = out.loc[is_wslh, "value"] * 1000  # mg -> ug
    out["item"] = out["item"].str.replace(".WSLH", "", regex=False)
    return out


def build_temp() -> pd.DataFrame:
    print("Loading physical limnology (temp/DO/light) - package 29 ...")
    raw = clean_negatives(load_latest_package("29"), "29")
    out = melt_value_flag_columns(raw, ID_CANDIDATES, exclude_bad_flags=True)
    out["source"] = 29
    return out


def build_secchi() -> pd.DataFrame:
    print("Loading secchi - package 31 ...")
    raw = clean_negatives(load_latest_package("31"), "31")
    id_cols = [c for c in ["lakeid", "year4", "daynum", "sampledate", "sta"] if c in raw.columns]
    value_cols = [c for c in raw.columns if c not in id_cols]
    out = raw.melt(id_vars=id_cols, value_vars=value_cols, var_name="item", value_name="value")
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    out = out[out["value"].notna() & (out["value"] >= 0)]
    out["depth"] = 0
    out["rep"] = 1
    out["source"] = 31
    return out


def build_nutrients() -> pd.DataFrame:
    print("Loading nutrients - package 1 ...")
    raw = split_wslh(clean_negatives(load_latest_package("1"), "1"))
    out = convert_wslh(melt_value_flag_columns(raw, ID_CANDIDATES, exclude_bad_flags=True))
    out["source"] = 1
    return out


def build_ions() -> pd.DataFrame:
    print("Loading major ions - package 2 ...")
    raw = clean_negatives(load_latest_package("2"), "2")
    raw = raw.rename(columns={c: c.replace("_sloh", ".sloh").replace("_n", ".n") for c in raw.columns})
    out = melt_value_flag_columns(raw, ID_CANDIDATES, exclude_bad_flags=True)
    out["source"] = 2
    return out


def build_ice() -> pd.DataFrame:
    """
    Ice duration, ice-on and ice-off (packages 32 north + 33 south).
    `year` in both packages is the calendar year of the spring thaw.
      iceduration - days of ice cover (summed if a winter has several rows)
      iceon       - first freeze, as day of year counted from Jan 1 of the
                    preceding (fall) year, so a Jan 5 freeze-up = 370
      iceoff      - last thaw, day of year
    """
    print("Loading ice duration/phenology - packages 32 (north) + 33 (south) ...")
    frames = []
    for pkg in ("32", "33"):
        df = load_latest_package(pkg)
        df = df[["lakeid", "year", "ice_on", "ice_off", "duration"]].copy()
        df["source"] = int(pkg)
        frames.append(df)
    ice = pd.concat(frames, ignore_index=True)
    ice = ice[ice["year"].notna()].copy()
    ice["year"] = ice["year"].astype(int)
    ice["ice_on"] = pd.to_datetime(ice["ice_on"], errors="coerce")
    ice["ice_off"] = pd.to_datetime(ice["ice_off"], errors="coerce")
    fall_jan1 = pd.to_datetime((ice["year"] - 1).astype(str) + "-01-01")
    ice["iceon"] = (ice["ice_on"] - fall_jan1).dt.days + 1
    ice["iceoff"] = ice["ice_off"].dt.dayofyear

    multi = ice.groupby(["lakeid", "year"]).size()
    if (multi > 1).any():
        print(f"  {int((multi > 1).sum())} lake-winters have more than one ice row; "
              "summing duration, using first ice-on and last ice-off")
    agg = ice.groupby(["lakeid", "year"], as_index=False).agg(
        duration=("duration", lambda s: s.sum(min_count=1)),
        iceon=("iceon", "min"),
        iceoff=("iceoff", "max"),
        source=("source", "first"),
    )
    out = []
    for item, col in (("iceduration", "duration"), ("iceon", "iceon"), ("iceoff", "iceoff")):
        rows = annual_rows(agg, item, "year", col)
        rows["source"] = agg.loc[rows.index, "source"].values
        out.append(rows)
    return pd.concat(out, ignore_index=True)


# ---------------------------------------------------------------------------
# Zooplankton (37 north, 90 south) - density and biomass by group
# ---------------------------------------------------------------------------

ZOOP_GROUP_MAP = {2: "cyclopoid", 3: "calanoid", 5: "cladocera", 6: "rotifer"}


def _norm_name(s) -> str:
    s = str(s).upper().encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Z ]", "", s).strip()


def _compile_mass_formula(formula):
    """
    Package 376 stores spreadsheet formulas such as "=3.0093*(C2^1.7064)",
    where the cell reference is the length (mm). Returns f(L) -> ug, or None.
    """
    if not isinstance(formula, str) or not formula.strip():
        return None
    expr = formula.strip().lstrip("=")
    expr = re.sub(r"\$?[A-Z]{1,2}\$?\d+", "L", expr)
    expr = expr.replace("^", "**")
    expr = re.sub(r"\bEXP\(", "math.exp(", expr, flags=re.I)
    expr = re.sub(r"\bLN\(", "math.log(", expr, flags=re.I)
    expr = re.sub(r"\bPI\(\)", "math.pi", expr, flags=re.I)
    if re.search(r"[^0-9Lmathexplogi.\s*+\-/(),eE]", expr):
        return None
    try:
        code = compile(expr, "<mass_formula>", "eval")
        eval(code, {"__builtins__": {}, "math": math}, {"L": 1.0})  # noqa: S307 - sanitized above
    except Exception:  # noqa: BLE001
        return None
    return lambda L, _c=code: eval(_c, {"__builtins__": {}, "math": math}, {"L": float(L)})  # noqa: S307


def load_mass_table() -> dict:
    """{normalized species name: (default length mm, f(L)->ug)} from package 376."""
    print("Loading zooplankton length-mass formulas - package 376 ...")
    t = load_latest_package("376")
    table = {}
    bad = []
    for _, r in t.iterrows():
        f = _compile_mass_formula(r.get("mass_formula"))
        if f is None:
            bad.append(r.get("species"))
            continue
        L = pd.to_numeric(r.get("length"), errors="coerce")
        table[_norm_name(r["species"])] = (None if pd.isna(L) else float(L), f)
    if bad:
        print(f"  {len(bad)} species in 376 have no usable formula: {bad}")
    return table


# Taxa in 37/90 whose names differ from package 376: map to the 376 genus
ZOOP_GENUS_ALIASES = {
    "BOSMINIDAE": "BOSMINA",
    "SINOBOSMINA": "BOSMINA",  # Sinobosmina fryei, formerly reported as Bosmina
}


def _lookup_mass(name: str, group: str, table: dict):
    n = _norm_name(name)
    if n in table:
        return table[n], "species"
    genus = n.split(" ")[0] if n else ""
    genus = ZOOP_GENUS_ALIASES.get(genus, genus)
    for key in (genus, ):
        if key in table:
            return table[key], "genus"
    same_genus = [k for k in table if k.split(" ")[0] == genus]
    if genus and same_genus:
        return table[sorted(same_genus)[0]], "genus"
    if group.upper() in table:
        return table[group.upper()], "group"
    return None, None


def build_zoops() -> pd.DataFrame:
    """
    Density (#/L) and biomass (ug dry mass/L) per major group.
    Package 37 reports #/L; package 90 reports #/m2 from a vertical tow, so
    it is divided by tow depth (m) x 1000 L/m3 to give #/L. (Before this fix
    the viewer plotted the Madison lakes' #/m2 values on a #/L axis.)
    """
    print("Loading zooplankton - packages 37 (north) + 90 (south) ...")
    north = load_latest_package("37")
    north["source"] = 37
    south = load_latest_package("90")
    south["source"] = 90
    south["density"] = pd.to_numeric(south["density"], errors="coerce") / (
        pd.to_numeric(south["towdepth"], errors="coerce") * 1000)

    keys = ["lakeid", "sample_date", "species_code"]
    dup = south.duplicated(keys, keep=False).sum()
    if dup:
        print(f"  package 90: {dup} rows share lake/date/species; averaging duplicates (replicate tows)")
    south = south.groupby(keys + ["year4", "species_name", "source"], as_index=False, dropna=False).agg(
        density=("density", "mean"), avg_length=("avg_length", "mean"))

    zoop = pd.concat([north, south], ignore_index=True)
    zoop["lakeid"] = zoop["lakeid"].astype(str).str.upper().str.strip()
    zoop["code"] = (pd.to_numeric(zoop["species_code"], errors="coerce") // 10000)
    zoop = zoop[zoop["code"].isin(ZOOP_GROUP_MAP.keys())].copy()
    zoop["group"] = zoop["code"].astype(int).map(ZOOP_GROUP_MAP)
    zoop["sampledate"] = pd.to_datetime(zoop["sample_date"], errors="coerce")
    zoop["density"] = pd.to_numeric(zoop["density"], errors="coerce")
    zoop = zoop[zoop["density"].notna() & (zoop["density"] >= 0) & zoop["sampledate"].notna()]

    # ---- biomass ----
    try:
        table = load_mass_table()
    except Exception as exc:  # noqa: BLE001
        print(f"  !! could not load package 376, skipping biomass: {exc}", file=sys.stderr)
        table = {}

    zoop["avg_length"] = pd.to_numeric(zoop["avg_length"], errors="coerce")
    # Recent years of 37 (2023-) and 90 (2024) report avg_length in micrometers
    # (medians ~600-800) although the metadata says mm. No crustacean
    # zooplankton is > 20 mm, so larger values are treated as um and converted.
    too_long = zoop["avg_length"] > ZOOP_LENGTH_MAX_MM
    if too_long.any():
        for pkg, sub in zoop[too_long].groupby("source"):
            qc_log_rows(sub, pkg, "avg_length", "converted",
                        f"zooplankton length > {ZOOP_LENGTH_MAX_MM} 'mm'; looks like micrometers, divided by 1000",
                        value_col="avg_length", taxon_col="species_name")
        print(f"  QC avg_length: {int(too_long.sum())} lengths > {ZOOP_LENGTH_MAX_MM} treated as um and converted to mm")
    zoop.loc[too_long, "avg_length"] = zoop.loc[too_long, "avg_length"] / 1000
    zoop.loc[zoop["avg_length"] <= 0, "avg_length"] = float("nan")

    mass = []
    unmatched = set()
    cache = {}
    for name, group, L in zip(zoop["species_name"], zoop["group"], zoop["avg_length"]):
        key = (name, group)
        if key not in cache:
            cache[key] = _lookup_mass(name, group, table) if table else (None, None)
        entry, _how = cache[key]
        if entry is None:
            unmatched.add(str(name))
            mass.append(float("nan"))
            continue
        ref_len, f = entry
        length = L if pd.notna(L) else ref_len
        try:
            mass.append(f(length) if length is not None else float("nan"))
        except Exception:  # noqa: BLE001
            mass.append(float("nan"))
    zoop["mass_ug"] = mass
    zoop["biomass"] = zoop["density"] * zoop["mass_ug"]
    if table and unmatched:
        print(f"  {len(unmatched)} zooplankton names had no length-mass formula (excluded from biomass): "
              f"{sorted(unmatched)}")
        for n in sorted(unmatched):
            QC_LOG.append({"package": 376, "variable": "biomass", "taxon": n, "action": "review",
                           "reason": "no matching length-mass formula in package 376"})

    out = []
    for value_col, suffix in (("density", ""), ("biomass", "_bm")):
        agg = (zoop.dropna(subset=[value_col])
               .groupby(["lakeid", "sampledate", "group", "source"], as_index=False)[value_col].sum())
        agg["item"] = agg["group"] + suffix
        agg["value"] = agg[value_col]
        agg["year4"] = agg["sampledate"].dt.year
        agg["daynum"] = agg["sampledate"].dt.dayofyear
        agg["depth"] = 0
        agg["rep"] = 1
        out.append(agg[["lakeid", "year4", "daynum", "sampledate", "depth", "rep", "item", "value", "source"]])
    return pd.concat(out, ignore_index=True)


# ---------------------------------------------------------------------------
# Phase 1 additions (standard / annual kinds)
# ---------------------------------------------------------------------------


def _depth_label(v):
    """'0-2' / '0-2m' stay labels (integrated samples); '4' -> 4.0."""
    s = str(v).strip().lower().replace("m", "").replace(" ", "")
    try:
        return float(s)
    except ValueError:
        return s


def build_chlorophyll() -> pd.DataFrame:
    """
    chlor / phaeo (ug/L).
    Trout Lake area (35): discrete depths, flags as in packages 1/2/29.
    Madison lakes (38): corrected fluorometric chlorophyll only (no
    gap-filling with uncorrected values); depth_range_m kept as a label for
    integrated samples ("0-2", "0-8") or a number for discrete depths.
    """
    print("Loading chlorophyll - packages 35 (north) + 38 (south) ...")
    north = clean_negatives(load_latest_package("35"), "35")
    north = north[[c for c in north.columns if c in ID_CANDIDATES + ["chlor", "phaeo", "flagchlor", "flagphaeo"]]]
    n_out = melt_value_flag_columns(north, ID_CANDIDATES, exclude_bad_flags=True)
    n_out["source"] = 35

    south = clean_negatives(load_latest_package("38"), "38")
    south["depth"] = south["depth_range_m"].map(_depth_label)
    frames = [n_out]
    for item, col in (("chlor", "correct_chl_fluor"), ("phaeo", "phaeo_fluor")):
        frames.append(to_standard(south, item, col, depth="depth", rep="rep", source=38))
    return pd.concat(frames, ignore_index=True)


def build_light_extinction() -> pd.DataFrame:
    print("Loading light extinction - package 259 ...")
    raw = load_latest_package("259")
    return to_standard(raw, "extcoef", "extcoef", source=259)


def build_snow_ice() -> pd.DataFrame:
    print("Loading snow and ice depth - package 34 ...")
    raw = clean_negatives(load_latest_package("34"), "34")
    return pd.concat([to_standard(raw, v, v, source=34) for v in ("avsnow", "totice", "whiteice", "blueice")],
                     ignore_index=True)


def build_lake_levels() -> pd.DataFrame:
    print("Loading lake levels - package 30 (manual readings) ...")
    raw = load_latest_package("30", entity=0)  # entity 1 is the 30-minute logger series
    return to_standard(raw, "llevel", "llevel_elevation", source=30)


def build_fish_richness() -> pd.DataFrame:
    print("Loading fish species richness - package 245 ...")
    return annual_rows(load_latest_package("245"), "fishrichness", "year4", "richness", source=245)


# ---------------------------------------------------------------------------
# Phase 2: Lake Kegonsa & Lake Waubesa (401)
# ---------------------------------------------------------------------------


def build_kegonsa_waubesa() -> pd.DataFrame:
    print("Loading Kegonsa / Waubesa physical & chemical limnology - package 401 ...")
    raw = split_wslh(clean_negatives(load_latest_package("401"), "401"))
    out = convert_wslh(melt_value_flag_columns(raw, ID_CANDIDATES, exclude_bad_flags=True))
    out["source"] = 401
    return out


# ---------------------------------------------------------------------------
# Phase 3 additions (standard kind)
# ---------------------------------------------------------------------------

PHYTO_DIVISIONS = {
    "Cyanophyta": "phyto_cyano",
    "Bacillariophyta": "phyto_diatom",
    "Chlorophyta": "phyto_green",
    "Cryptophyta": "phyto_crypto",
    "Chrysophyta": "phyto_chryso",
    "Pyrrhophyta": "phyto_dino",
}


def build_phytoplankton() -> pd.DataFrame:
    """Madison lakes phytoplankton (88): biomass (mg/L) by division and total."""
    print("Loading phytoplankton - package 88 (Madison lakes) ...")
    raw = load_latest_package("88")
    raw["biomass_conc"] = pd.to_numeric(raw["biomass_conc"], errors="coerce")
    raw["depth"] = raw["depth_range"].map(_depth_label)
    keys = ["lakeid", "sampledate", "depth"]
    frames = []
    total = raw.groupby(keys, as_index=False)["biomass_conc"].sum()
    frames.append(to_standard(total, "phyto_total", "biomass_conc", depth="depth", source=88))
    by_div = raw.groupby(keys + ["division"], as_index=False)["biomass_conc"].sum()
    for div, item in PHYTO_DIVISIONS.items():
        sub = by_div[by_div["division"] == div]
        if len(sub):
            frames.append(to_standard(sub, item, "biomass_conc", depth="depth", source=88))
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Taxon-kind datasets: one value per lake-year-taxon
# ---------------------------------------------------------------------------


def build_pelagic_inverts() -> pd.DataFrame:
    """
    Pelagic macroinvertebrates (14): individuals per m2 at the deep hole
    (stations 1-2), using the deepest tow each year (whole water column),
    averaged across deep-hole stations.
    """
    print("Loading pelagic macroinvertebrates - package 14 ...")
    raw = load_latest_package("14")
    raw["lakeid"] = raw["lakeid"].astype(str).str.upper()
    raw = raw[raw["sta"].isin([1, 2])].copy()
    raw["avg_ind_per_m2"] = pd.to_numeric(raw["avg_ind_per_m2"], errors="coerce")
    idx = raw.groupby(["lakeid", "year4", "sta", "taxon"])["depth"].idxmax()
    deepest = raw.loc[idx]
    out = deepest.groupby(["lakeid", "year4", "taxon"], as_index=False)["avg_ind_per_m2"].mean()
    out = out.rename(columns={"avg_ind_per_m2": "value"})
    out["taxon"] = out["taxon"].str.title()
    return out


def build_benthic_inverts() -> pd.DataFrame:
    """
    Benthic macroinvertebrates (11), Hester-Dendy artificial substrate
    samplers: mean individuals per sampler, by order (from the taxonomic
    code table) plus all taxa combined. A sampler = lake/year/site/rep.
    """
    print("Loading benthic macroinvertebrates - package 11 ...")
    raw = load_latest_package("11", entity=0)
    codes = load_latest_package("11", entity="Taxonomic Codes")
    raw["lakeid"] = raw["lakeid"].astype(str).str.upper().str.strip()
    raw["site"] = raw["site"].astype(str).str.upper().str.strip().replace({"G": "GILL"})
    raw["number_indiv"] = pd.to_numeric(raw["number_indiv"], errors="coerce")

    def group_name(r):
        for col in ("order_t", "class", "phylum"):
            v = r.get(col)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    codes["group"] = codes.apply(group_name, axis=1)
    raw = raw.merge(codes[["taxon_code", "group"]], on="taxon_code", how="left")
    raw["group"] = raw["group"].fillna(raw["description"].astype(str).str.strip())

    sample_keys = ["lakeid", "year4", "site", "rep"]
    n_samplers = raw.drop_duplicates(sample_keys).groupby(["lakeid", "year4"]).size().rename("n_samplers")
    counted = raw[raw["number_indiv"].notna()]
    frames = []
    for taxon, sub in [("All taxa", counted.assign(group="All taxa"))] + [(None, counted)]:
        s = sub.groupby(["lakeid", "year4", "group"], as_index=False)["number_indiv"].sum()
        frames.append(s)
    out = pd.concat(frames, ignore_index=True).merge(n_samplers, on=["lakeid", "year4"])
    out["value"] = out["number_indiv"] / out["n_samplers"]
    out = out.rename(columns={"group": "taxon"})
    return out[["lakeid", "year4", "taxon", "value", "n_samplers"]].rename(columns={"n_samplers": "n"})


def build_madison_macrophytes() -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Madison lakes macrophytes.
      macrofreq - % of rake samples containing each species (23), out of all
                  rake samples taken that lake-year (sample list from 24 +
                  23). Rating data only record presences, so this avoids
                  overstating cover.
      macrowt   - mean plant wet weight per rake sample (g), package 24.
    """
    print("Loading Madison macrophytes - packages 23 + 24 ...")
    rating = load_latest_package("23")
    weight = load_latest_package("24")
    keys = ["lakeid", "year4", "sampledate", "transect", "depth", "rep"]
    for df in (rating, weight):
        df["lakeid"] = df["lakeid"].astype(str).str.upper()
    samples = pd.concat([rating[keys], weight[keys]]).drop_duplicates()
    n = samples.groupby(["lakeid", "year4"]).size().rename("n")
    present = rating[pd.to_numeric(rating["rake_rating"], errors="coerce") >= 1]
    hits = present.drop_duplicates(keys + ["spname"]).groupby(["lakeid", "year4", "spname"]).size().rename("hits")
    freq = hits.reset_index().merge(n.reset_index(), on=["lakeid", "year4"])
    freq["value"] = 100 * freq["hits"] / freq["n"]
    freq = freq.rename(columns={"spname": "taxon"})[["lakeid", "year4", "taxon", "value", "n"]]

    weight["plant_wt_hand"] = pd.to_numeric(weight["plant_wt_hand"], errors="coerce")
    neg = weight["plant_wt_hand"] < 0
    if neg.any():
        qc_log_rows(weight[neg], 24, "plant_wt_hand", "remove", "negative plant weight", value_col="plant_wt_hand")
    w = weight[weight["plant_wt_hand"] >= 0]
    wt = w.groupby(["lakeid", "year4"], as_index=False)["plant_wt_hand"].mean()
    return freq, annual_rows(wt, "macrowt", "year4", "plant_wt_hand", source=24)


def build_trout_macrophytes() -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Trout Lake macrophytes.
      trmacro     - dry biomass (g/m2) by species (25; reported per 0.25 m2,
                    x4). Species absent from a site/depth count as zero, so
                    each value = sum over sampled site-depths / number sampled.
      trrichness  - mean species per quadrat (28).
    """
    print("Loading Trout Lake macrophytes - packages 25 + 28 ...")
    bio = load_latest_package("25", entity=0)
    bio["avg_dryweight_g"] = pd.to_numeric(bio["avg_dryweight_g"], errors="coerce")
    neg = bio["avg_dryweight_g"] < 0
    if neg.any():
        qc_log_rows(bio[neg], 25, "avg_dryweight_g", "remove", "negative dry weight",
                    value_col="avg_dryweight_g", taxon_col="species_name")
    bio = bio[bio["avg_dryweight_g"] >= 0].copy()
    bio["gm2"] = bio["avg_dryweight_g"] * 4
    combos = bio.drop_duplicates(["year4", "site", "depth"]).groupby("year4").size().rename("n")
    sp = bio.groupby(["year4", "species_name"], as_index=False)["gm2"].sum()
    tot = bio.groupby("year4", as_index=False)["gm2"].sum().assign(species_name="All species")
    b = pd.concat([sp, tot]).merge(combos.reset_index(), on="year4")
    b["value"] = b["gm2"] / b["n"]
    b["lakeid"] = "TR"
    b = b.rename(columns={"species_name": "taxon"})[["lakeid", "year4", "taxon", "value", "n"]]

    rich = load_latest_package("28", entity=0)
    rich["richness"] = pd.to_numeric(rich["richness"], errors="coerce")
    r = rich.dropna(subset=["richness"]).groupby(["lakeid", "year4"], as_index=False)["richness"].mean()
    return b, annual_rows(r, "trrichness", "year4", "richness", source=28)


# ---------------------------------------------------------------------------
# Crayfish / fish (catch kind) - mirrors loadLTERcrayfish / loadLTERfishabundance
# ---------------------------------------------------------------------------

GEAR_NAMES = {
    "BSEINE": "Beach Seine (ended 2019)",
    "CRAYTR": "Crayfish Trap",
    "ELFISH": "Electrofishing",
    "FYKNET": "Fyke Net",
    "TRAMML": "Trammel Net",
    "VGN": "Vertical Gill Net",
    "MINNOW": "Minnow Trap",
}


def recode_gear(g: pd.Series) -> pd.Series:
    g = g.astype(str).str.strip()
    g = g.where(~g.str.startswith("VGN"), "VGN")
    g = g.where(~g.str.startswith("FYKNE"), "FYKNET")  # FYKNED / FYKNEL are fyke-net variants
    return g.map(GEAR_NAMES).fillna(g)


def build_crayfish() -> pd.DataFrame:
    print("Loading crayfish abundance - package 3 ...")
    raw = load_latest_package("3")
    out = raw[raw["total_caught"].notna() & raw["spname"].notna()].copy()
    out["CPUE"] = out["total_caught"] / out["effort"]
    out["item"] = "rusty"
    out = out[out["spname"] != "CRAYFISH"]
    out = out[out["gearid"] == "CRAYTR"]  # server.R keeps only crayfish-trap catches
    out["gearid"] = out["gearid"].map(GEAR_NAMES).fillna(out["gearid"])
    return out


def build_fish() -> pd.DataFrame:
    print("Loading fish abundance - package 7 ...")
    raw = load_latest_package("7")
    out = raw[raw["total_caught"].notna() & raw["spname"].notna()].copy()
    out["CPUE"] = out["total_caught"] / out["effort"]
    out["item"] = "fish"
    out = out[out["spname"] != "UNIDENTIFIED"]
    out = out[~out["gearid"].isin(["ESHOCK", "MINNOW"])]
    out["gearid"] = out["gearid"].where(~out["gearid"].str.contains("VGN"), "VGN")
    out["gearid"] = out["gearid"].map(GEAR_NAMES).fillna(out["gearid"])
    # server.R: group_by(spname) |> filter(n() > 25)
    counts = out.groupby("spname")["spname"].transform("count")
    return out[counts > 25]


def build_fish_lengths() -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Fish lengths & weights (6), summarized per lake-year-species-gear:
    value = median, lo/hi = 25th/75th percentiles, n = fish measured.
    Package 181 (Wingra) duplicates the Wingra rows of package 6 and is not used.
    """
    print("Loading fish lengths and weights - package 6 ...")
    raw = load_latest_package("6")
    raw["lakeid"] = raw["lakeid"].astype(str).str.upper()
    raw = raw[raw["spname"].notna() & (raw["spname"] != "UNIDENTIFIED")].copy()
    raw["gear"] = recode_gear(raw["gearid"])
    raw["length"] = pd.to_numeric(raw["length"], errors="coerce")
    raw["weight"] = pd.to_numeric(raw["weight"], errors="coerce")
    raw["depth"] = pd.to_numeric(raw["depth"], errors="coerce")

    bad_len = raw["length"].notna() & ((raw["length"] <= 0) | (raw["length"] > FISH_LENGTH_MAX_MM))
    if bad_len.any():
        qc_log_rows(raw[bad_len], 6, "length", "remove",
                    f"fish length <= 0 or > {FISH_LENGTH_MAX_MM} mm", value_col="length", taxon_col="spname")
    zero_wt = raw["weight"].notna() & (raw["weight"] <= 0)
    if zero_wt.any():
        qc_log_rows(raw[zero_wt], 6, "weight", "remove", "fish weight <= 0 g (treated as missing)",
                    value_col="weight", taxon_col="spname")
    deep = raw["depth"] > 40  # deepest core lake (Trout) is ~36 m
    if deep.any():
        qc_log_rows(raw[deep], 6, "depth", "review", "fish sample depth > 40 m (not used by viewer)",
                    value_col="depth", taxon_col="spname")
    print(f"  QC fish: {int(bad_len.sum())} lengths and {int(zero_wt.sum())} weights removed; "
          f"{int(deep.sum())} depths > 40 m logged")
    raw.loc[bad_len, "length"] = float("nan")
    raw.loc[zero_wt, "weight"] = float("nan")

    def summarize(col):
        d = raw.dropna(subset=[col])
        g = d.groupby(["lakeid", "year4", "spname", "gear"])[col]
        s = pd.DataFrame({
            "value": g.median(), "lo": g.quantile(0.25), "hi": g.quantile(0.75), "n": g.size(),
        }).reset_index()
        s = s[s["n"] >= 3].rename(columns={"spname": "taxon"})
        for c in ("value", "lo", "hi"):
            s[c] = s[c].round(1)
        return s

    return summarize("length"), summarize("weight")


def build_length_frequency() -> pd.DataFrame:
    """Fish length frequency (8): wide (one column per species) -> long, 5 mm bins."""
    print("Loading fish length frequency - package 8 ...")
    raw = load_latest_package("8")
    raw["lakeid"] = raw["lakeid"].astype(str).str.upper()
    species_cols = [c for c in raw.columns if c not in ("lakeid", "year4", "bin_midpoint")]
    long = raw.melt(id_vars=["lakeid", "year4", "bin_midpoint"], value_vars=species_cols,
                    var_name="taxon", value_name="n")
    long["n"] = pd.to_numeric(long["n"], errors="coerce")
    long = long[long["n"] > 0].copy()
    long["taxon"] = long["taxon"].str.upper()
    long["n"] = long["n"].round(2)
    return long.rename(columns={"bin_midpoint": "bin"})[["lakeid", "year4", "taxon", "bin", "n"]]


# ---------------------------------------------------------------------------
# Variable registry -> data/matchtable.json
#   (var, display name, group, kind, source packages, extra fields)
# Group order here is the order of the viewer's dropdown.
# ---------------------------------------------------------------------------

STANDARD, ANNUAL, CATCH, TAXON, LENGTHFREQ = "standard", "annual", "catch", "taxon", "lengthfreq"

REGISTRY = [
    # Physical
    ("wtemp", "Water Temperature (°C)", "Physical", STANDARD, [29, 401]),
    ("o2", "Dissolved Oxygen (mg/L)", "Physical", STANDARD, [29, 401]),
    ("o2sat", "Dissolved Oxygen (% sat)", "Physical", STANDARD, [29, 401]),
    ("llevel", "Lake Level (m above sea level)", "Physical", STANDARD, [30]),
    # Ice & snow
    ("iceduration", "Lake Ice Duration (days)", "Ice & Snow", ANNUAL, [32, 33]),
    ("iceon", "Ice-On (day of year; >365 = January)", "Ice & Snow", ANNUAL, [32, 33]),
    ("iceoff", "Ice-Off (day of year)", "Ice & Snow", ANNUAL, [32, 33]),
    ("totice", "Total Ice Thickness (cm)", "Ice & Snow", STANDARD, [34]),
    ("whiteice", "White Ice Thickness (cm)", "Ice & Snow", STANDARD, [34]),
    ("blueice", "Blue Ice Thickness (cm)", "Ice & Snow", STANDARD, [34]),
    ("avsnow", "Snow Depth on Ice (cm)", "Ice & Snow", STANDARD, [34]),
    # Light & clarity
    ("secview", "Secchi Depth (m) with viewer", "Light & Clarity", STANDARD, [31]),
    ("secnview", "Secchi Depth (m) without viewer", "Light & Clarity", STANDARD, [31]),
    ("extcoef", "Light Extinction Coefficient (m⁻¹)", "Light & Clarity", STANDARD, [259]),
    ("light", "Underwater Light (µmol/m²/s)", "Light & Clarity", STANDARD, [29]),
    ("deck", "Surface Light (\u00b5mol/m\u00b2/s)", "Light & Clarity", STANDARD, [29]),
    # Nutrients & carbon
    ("doc", "Dissolved Organic Carbon (mg/L)", "Nutrients & Carbon", STANDARD, [1]),
    ("dic", "Dissolved Inorganic Carbon (mg/L)", "Nutrients & Carbon", STANDARD, [1]),
    ("toc", "Total Organic Carbon (mg/L)", "Nutrients & Carbon", STANDARD, [1]),
    ("tic", "Total Inorganic Carbon (mg/L)", "Nutrients & Carbon", STANDARD, [1]),
    ("no3no2", "Nitrate + Nitrite as N (µg/L)", "Nutrients & Carbon", STANDARD, [1, 401]),
    ("no2", "Nitrite as N (µg/L)", "Nutrients & Carbon", STANDARD, [1]),
    ("nh4", "Ammonium as N (µg/L)", "Nutrients & Carbon", STANDARD, [1, 401]),
    ("kjdl_n", "Total Kjeldahl Nitrogen (µg/L)", "Nutrients & Carbon", STANDARD, [1, 401]),
    ("totnuf", "Total Nitrogen (µg/L) unfiltered", "Nutrients & Carbon", STANDARD, [1, 401]),
    ("totnf", "Total Nitrogen (µg/L) filtered", "Nutrients & Carbon", STANDARD, [1]),
    ("drp", "Dissolved Reactive Phosphorus (µg/L)", "Nutrients & Carbon", STANDARD, [1, 401]),
    ("totpuf", "Total Phosphorus (µg/L) unfiltered", "Nutrients & Carbon", STANDARD, [1, 401]),
    ("totpf", "Total Phosphorus (µg/L) filtered", "Nutrients & Carbon", STANDARD, [1]),
    ("drsif", "Dissolved Reactive Silica (µg/L)", "Nutrients & Carbon", STANDARD, [1, 401]),
    ("brsif", "Bicarbonate Reactive Silica (µg/L) filtered", "Nutrients & Carbon", STANDARD, [1]),
    ("brsiuf", "Bicarbonate Reactive Silica (µg/L) unfiltered", "Nutrients & Carbon", STANDARD, [1]),
    ("tpm", "Total Particulate Matter (mg/L)", "Nutrients & Carbon", STANDARD, [1]),
    # Ions
    ("ph", "pH", "Ions", STANDARD, [1, 401]),
    ("phair", "pH (air equilibrated)", "Ions", STANDARD, [1, 401]),
    ("alk", "Alkalinity (µeq/L)", "Ions", STANDARD, [1, 401]),
    ("ca", "Calcium (mg/L)", "Ions", STANDARD, [2]),
    ("mg", "Magnesium (mg/L)", "Ions", STANDARD, [2]),
    ("na", "Sodium (mg/L)", "Ions", STANDARD, [2]),
    ("k", "Potassium (mg/L)", "Ions", STANDARD, [2]),
    ("so4", "Sulfate (mg/L)", "Ions", STANDARD, [2, 401]),
    ("cl", "Chloride (mg/L)", "Ions", STANDARD, [2, 401]),
    ("fe", "Iron (mg/L)", "Ions", STANDARD, [2]),
    ("mn", "Manganese (mg/L)", "Ions", STANDARD, [2]),
    ("cond", "Specific Conductance (µS/cm)", "Ions", STANDARD, [2]),
    # Algae
    ("chlor", "Chlorophyll a (µg/L)", "Algae", STANDARD, [35, 38]),
    ("phaeo", "Phaeophytin (µg/L)", "Algae", STANDARD, [35, 38]),
    ("phyto_total", "Phytoplankton Biomass, Total (mg/L)", "Algae", STANDARD, [88]),
    ("phyto_cyano", "Phytoplankton Biomass, Cyanobacteria (mg/L)", "Algae", STANDARD, [88]),
    ("phyto_diatom", "Phytoplankton Biomass, Diatoms (mg/L)", "Algae", STANDARD, [88]),
    ("phyto_green", "Phytoplankton Biomass, Green Algae (mg/L)", "Algae", STANDARD, [88]),
    ("phyto_crypto", "Phytoplankton Biomass, Cryptophytes (mg/L)", "Algae", STANDARD, [88]),
    ("phyto_chryso", "Phytoplankton Biomass, Golden Algae (mg/L)", "Algae", STANDARD, [88]),
    ("phyto_dino", "Phytoplankton Biomass, Dinoflagellates (mg/L)", "Algae", STANDARD, [88]),
    # Zooplankton
    ("cladocera", "Cladocera (#/L)", "Zooplankton", STANDARD, [37, 90]),
    ("calanoid", "Calanoid copepod (#/L)", "Zooplankton", STANDARD, [37, 90]),
    ("cyclopoid", "Cyclopoid copepod (#/L)", "Zooplankton", STANDARD, [37, 90]),
    ("rotifer", "Rotifer (#/L)", "Zooplankton", STANDARD, [37, 90]),
    ("cladocera_bm", "Cladocera biomass (µg/L)", "Zooplankton", STANDARD, [37, 90, 376]),
    ("calanoid_bm", "Calanoid copepod biomass (µg/L)", "Zooplankton", STANDARD, [37, 90, 376]),
    ("cyclopoid_bm", "Cyclopoid copepod biomass (µg/L)", "Zooplankton", STANDARD, [37, 90, 376]),
    ("rotifer_bm", "Rotifer biomass (µg/L)", "Zooplankton", STANDARD, [37, 90, 376]),
    # Macroinvertebrates
    ("pelagicinv", "Pelagic Macroinvertebrates (#/m²)", "Macroinvertebrates", TAXON, [14],
     {"taxonLabel": "Taxon", "defaultTaxon": "Chaoborus Larvae"}),
    ("benthicinv", "Benthic Macroinvertebrates (# per sampler)", "Macroinvertebrates", TAXON, [11],
     {"taxonLabel": "Taxonomic group", "defaultTaxon": "All taxa"}),
    ("rusty", "Crayfish (CPUE)", "Macroinvertebrates", CATCH, [3]),
    # Macrophytes
    ("macrofreq", "Macrophyte Frequency (% of rake samples)", "Macrophytes", TAXON, [23, 24],
     {"taxonLabel": "Species"}),
    ("macrowt", "Macrophyte Wet Weight per Rake Sample (g)", "Macrophytes", ANNUAL, [24]),
    ("trmacro", "Trout Lake Macrophyte Biomass (g dry/m²)", "Macrophytes", TAXON, [25],
     {"taxonLabel": "Species", "defaultTaxon": "All species"}),
    ("trrichness", "Trout Lake Macrophyte Richness (species/quadrat)", "Macrophytes", ANNUAL, [28]),
    # Fish
    ("fish", "Fish (CPUE)", "Fish", CATCH, [7]),
    ("fishrichness", "Fish Species Richness", "Fish", ANNUAL, [245]),
    ("fishlength", "Fish Length (mm; median & IQR)", "Fish", TAXON, [6],
     {"taxonLabel": "Species", "subLabel": "Gear", "defaultTaxon": "BLUEGILL"}),
    ("fishweight", "Fish Weight (g; median & IQR)", "Fish", TAXON, [6],
     {"taxonLabel": "Species", "subLabel": "Gear", "defaultTaxon": "BLUEGILL"}),
    ("fishlenfreq", "Fish Length Frequency (% of fish)", "Fish", LENGTHFREQ, [8],
     {"taxonLabel": "Species", "subLabel": "Year", "defaultTaxon": "BLUEGILL"}),
]


def build_matchtable(written: set) -> list[dict]:
    rows = []
    for entry in REGISTRY:
        var, name, group, kind, packages = entry[:5]
        extra = entry[5] if len(entry) > 5 else {}
        if var not in written:
            print(f"  (matchtable) skipping {var}: no data file written")
            continue
        rows.append({
            "var": var, "name": name, "group": group, "kind": kind,
            "packages": packages,
            "url": edi_url(packages[0]),  # kept for older viewer code
            **extra,
        })
    return rows


# ---------------------------------------------------------------------------
# Lake metadata -> data/lakelocations.json
# Coordinates/attributes for the 11 core lakes come from package 434 when it
# loads; Kegonsa and Waubesa are not in 434 (coordinates from NTL staff).
# ---------------------------------------------------------------------------

LAKE_META = [
    {"lakeid": "AL", "lake": "Allequash Lake", "region": "north", "lat": 46.038317, "long": -89.620617},
    {"lakeid": "BM", "lake": "Big Musky Lake", "region": "north", "lat": 46.021067, "long": -89.611783},
    {"lakeid": "CB", "lake": "Crystal Bog", "region": "north", "lat": 46.007583, "long": -89.606183},
    {"lakeid": "CR", "lake": "Crystal Lake", "region": "north", "lat": 46.00275, "long": -89.612233},
    {"lakeid": "SP", "lake": "Sparkling Lake", "region": "north", "lat": 46.007733, "long": -89.701183},
    {"lakeid": "TB", "lake": "Trout Bog", "region": "north", "lat": 46.04125, "long": -89.686283},
    {"lakeid": "TR", "lake": "Trout Lake", "region": "north", "lat": 46.029267, "long": -89.665017},
    {"lakeid": "ME", "lake": "Lake Mendota", "region": "south", "lat": 43.09885, "long": -89.40545},
    {"lakeid": "MO", "lake": "Lake Monona", "region": "south", "lat": 43.06337, "long": -89.36086},
    {"lakeid": "WI", "lake": "Lake Wingra", "region": "south", "lat": 43.05258, "long": -89.42499},
    {"lakeid": "FI", "lake": "Fish Lake", "region": "south", "lat": 43.28733, "long": -89.65173},
    # Kegonsa / Waubesa are not in package 434; area, max depth and trophic state
    # are WDNR lake-survey figures (3,209 / 2,074 acres; 31 / 38 ft max depth).
    {"lakeid": "KE", "lake": "Lake Kegonsa", "region": "south", "lat": 42.959722, "long": -89.245556,
     "area_ha": 1298.6, "max_depth_m": 9.4, "trophic_state": "eutrophic",
     "mixing_type": "dimictic"},
    {"lakeid": "WA", "lake": "Lake Waubesa", "region": "south", "lat": 43.008889, "long": -89.317778,
     "area_ha": 839.3, "max_depth_m": 11.6, "trophic_state": "eutrophic",
     "mixing_type": "dimictic"},
]
LAKEID_TO_NAME = {row["lakeid"]: row["lake"] for row in LAKE_META}
NAME_434_TO_ID = {**{row["lake"]: row["lakeid"] for row in LAKE_META}, "Big Muskellunge Lake": "BM"}
LAKE_434_FIELDS = {
    "max_depth_m": "max_depth_m", "avg_depth_m": "avg_depth_m", "waterbody_area_ha": "area_ha",
    "trophic_state": "trophic_state", "mixing_type": "mixing_type", "drainage_type": "drainage_type",
}


def build_lake_locations() -> list[dict]:
    lakes = [dict(r) for r in LAKE_META]
    try:
        print("Loading core lake information - package 434 ...")
        info = load_latest_package("434")
        by_id = {}
        for _, r in info.iterrows():
            lid = NAME_434_TO_ID.get(str(r.get("waterbody_name")).strip())
            if lid:
                by_id[lid] = r
            else:
                print(f"  !! 434 lake not matched to a viewer lake: {r.get('waterbody_name')}")
        for lake in lakes:
            r = by_id.get(lake["lakeid"])
            if r is None:
                continue
            if pd.notna(r.get("waterbody_lat_decdeg")) and pd.notna(r.get("waterbody_lon_decdeg")):
                lake["lat"] = float(r["waterbody_lat_decdeg"])
                lake["long"] = float(r["waterbody_lon_decdeg"])
            for src, dst in LAKE_434_FIELDS.items():
                v = r.get(src)
                if pd.notna(v):
                    lake[dst] = v.item() if hasattr(v, "item") else v
    except Exception as exc:  # noqa: BLE001
        print(f"  !! failed to load package 434, using built-in coordinates: {exc}", file=sys.stderr)
    return lakes


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

STANDARD_COLS = ["lakeid", "lakename", "year4", "daynum", "sampledate", "depth", "rep", "value"]


def add_lakename(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["lakeid"] = df["lakeid"].astype(str).str.upper().str.strip()
    df["lakename"] = df["lakeid"].map(LAKEID_TO_NAME)
    return df[df["lakename"].notna()]


def write_var(var: str, df: pd.DataFrame, written: list):
    os.makedirs("data/vars", exist_ok=True)
    out_path = f"data/vars/{var}.json"
    df.to_json(out_path, orient="records")
    print(f"  wrote {out_path} ({df.shape[0]:,} rows)")
    written.append(var)


def run_builder(label, builder):
    try:
        return builder()
    except Exception as exc:  # noqa: BLE001
        print(f"  !! failed to load {label}: {exc}", file=sys.stderr)
        return None


def main():
    has_updates, current_revisions = check_for_updates()
    if not has_updates:
        print("Skipping download and processing.")
        return

    written: list[str] = []

    # ---- standard + annual long-format data ------------------------------
    frames = []
    for label, builder in [
        ("temp", build_temp),
        ("secchi", build_secchi),
        ("nutrients", build_nutrients),
        ("ions", build_ions),
        ("ice", build_ice),
        ("zoops", build_zoops),
        ("chlorophyll", build_chlorophyll),
        ("light extinction", build_light_extinction),
        ("snow & ice", build_snow_ice),
        ("lake levels", build_lake_levels),
        ("fish richness", build_fish_richness),
        ("kegonsa/waubesa", build_kegonsa_waubesa),
        ("phytoplankton", build_phytoplankton),
    ]:
        df = run_builder(label, builder)
        if df is not None and len(df):
            frames.append(df)

    madison_macro = run_builder("madison macrophytes", build_madison_macrophytes)
    trout_macro = run_builder("trout lake macrophytes", build_trout_macrophytes)
    for res in (madison_macro, trout_macro):
        if res is not None:
            frames.append(res[1])

    if frames:
        all_lter = add_lakename(pd.concat(frames, ignore_index=True))
        all_lter["sampledate"] = pd.to_datetime(all_lter["sampledate"]).dt.strftime("%Y-%m-%d")
        all_lter = apply_qc_rules(all_lter)
        print(f"Combined standard dataset: {all_lter.shape[0]:,} rows")
        # Write one JSON file per variable so the browser only fetches what it needs.
        for var in sorted(all_lter["item"].unique()):
            subset = all_lter.loc[all_lter["item"] == var, STANDARD_COLS]
            write_var(var, subset, written)
    else:
        print("  !! no standard datasets loaded", file=sys.stderr)

    # ---- catch (CPUE) ----------------------------------------------------
    catch_frames = [df for df in (run_builder("crayfish", build_crayfish), run_builder("fish", build_fish))
                    if df is not None]
    if catch_frames:
        catch = pd.concat(catch_frames, ignore_index=True)
        catch = add_lakename(catch[catch["year4"] != 1981])  # mirrors filter(year4 != 1981) in server.R
        cols = ["lakeid", "lakename", "year4", "spname", "gearid", "total_caught", "effort", "CPUE"]
        for var in catch["item"].unique():
            write_var(var, catch.loc[catch["item"] == var, cols], written)

    # ---- taxon kind --------------------------------------------------------
    taxon_outputs = {
        "pelagicinv": run_builder("pelagic macroinvertebrates", build_pelagic_inverts),
        "benthicinv": run_builder("benthic macroinvertebrates", build_benthic_inverts),
        "macrofreq": madison_macro[0] if madison_macro is not None else None,
        "trmacro": trout_macro[0] if trout_macro is not None else None,
    }
    fish_len = run_builder("fish lengths", build_fish_lengths)
    if fish_len is not None:
        taxon_outputs["fishlength"], taxon_outputs["fishweight"] = fish_len
    for var, df in taxon_outputs.items():
        if df is None or not len(df):
            continue
        df = add_lakename(df)
        df["value"] = df["value"].round(3)
        cols = [c for c in ["lakeid", "lakename", "year4", "taxon", "gear", "value", "lo", "hi", "n"]
                if c in df.columns]
        write_var(var, df[cols], written)

    lf = run_builder("fish length frequency", build_length_frequency)
    if lf is not None and len(lf):
        write_var("fishlenfreq", add_lakename(lf)[["lakeid", "lakename", "year4", "taxon", "bin", "n"]], written)

    if not written:
        print("No datasets loaded successfully; aborting.", file=sys.stderr)
        sys.exit(1)

    with open("data/matchtable.json", "w") as f:
        json.dump(build_matchtable(set(written)), f, indent=2)
    with open("data/lakelocations.json", "w") as f:
        json.dump(build_lake_locations(), f, indent=2)
    write_qc_log()
    with open(TRACKING_FILE, "w") as f:
        json.dump({"last_download_date": datetime.now(timezone.utc).date().isoformat(),
                   "revisions": current_revisions}, f, indent=2)

    print(f"Wrote {len(written)} per-variable files to data/vars/, plus matchtable.json, "
          f"lakelocations.json, {QC_FILE} and {TRACKING_FILE}")


if __name__ == "__main__":
    main()
