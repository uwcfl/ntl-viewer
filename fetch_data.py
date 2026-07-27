"""
Fetches and processes North Temperate Lakes LTER data from the EDI data
repository (PASTA REST API), reproducing the logic of the original R/Shiny
app's server.R (loadLTERnutrients / loadLTERions / loadLTERtemp /
loadLTERsecchi / loadLTERice / loadLTERzoop + the pivoting/renaming steps
that build `allLTER`).

Run weekly by .github/workflows/update-data.yml. Writes three files consumed
by the static site:
  data/all_lter.json      - long-format observations
  data/matchtable.json    - variable code -> display name -> citation URL
  data/lakelocations.json - lake metadata + coordinates
"""

import io
import json
import os
import re
import sys
from datetime import date

import pandas as pd
import requests

PASTA_BASE = "https://pasta.lternet.edu/package"
SCOPE = "knb-lter-ntl"
HEADERS = {"User-Agent": "ntl-lter-viewer/1.0 (weekly data refresh)"}

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

# ---------------------------------------------------------------------------
# Generic EDI helpers
# ---------------------------------------------------------------------------


def newest_revision(identifier: str) -> str:
    url = f"{PASTA_BASE}/eml/{SCOPE}/{identifier}"
    r = requests.get(
        url,
        headers=HEADERS,
        params={"filter": "newest", "key": EDI_ACCESS_KEY},
        timeout=60,
    )
    r.raise_for_status()
    return r.text.strip()


def first_entity_id(identifier: str, revision: str) -> str:
    url = f"{PASTA_BASE}/data/eml/{SCOPE}/{identifier}/{revision}"
    r = requests.get(url, headers=HEADERS, params={"key": EDI_ACCESS_KEY}, timeout=60)
    r.raise_for_status()
    entity_ids = [line.strip() for line in r.text.splitlines() if line.strip()]
    if not entity_ids:
        raise RuntimeError(f"No data entities found for {SCOPE}.{identifier}.{revision}")
    return entity_ids[0]


def read_entity_csv(identifier: str, revision: str, entity_id: str) -> pd.DataFrame:
    url = f"{PASTA_BASE}/data/eml/{SCOPE}/{identifier}/{revision}/{entity_id}"
    r = requests.get(url, headers=HEADERS, params={"key": EDI_ACCESS_KEY}, timeout=180)
    r.raise_for_status()
    return pd.read_csv(io.StringIO(r.text), low_memory=False)


def load_latest_package(identifier: str) -> pd.DataFrame:
    """Fetch the newest revision's first data entity for a package id."""
    revision = newest_revision(identifier)
    entity_id = first_entity_id(identifier, revision)
    df = read_entity_csv(identifier, revision, entity_id)
    print(f"  loaded {SCOPE}.{identifier}.{revision} entity {entity_id} -> {df.shape}")
    return df


# ---------------------------------------------------------------------------
# Generic long-pivot helper (mirrors the repeated
# rename(value_/error_) -> pivot_longer -> flag-filter -> drop error pattern
# used throughout server.R)
# ---------------------------------------------------------------------------

ID_CANDIDATES = ["lakeid", "year4", "daynum", "sampledate", "depth", "rep", "sta", "event"]
BAD_FLAG_PATTERN = re.compile(r"[AKLHU]")  # flags to exclude, matches str_detect(error,'A|K|L|H'[|U'])


def melt_value_flag_columns(
    df: pd.DataFrame,
    id_cols: list[str],
    exclude_bad_flags: bool = True,
) -> pd.DataFrame:
    """
    Generic version of the R pattern:
      rename_at(vars(<values>), ~str_c("value_", .)) %>%
      rename_at(vars(<flags>),  ~str_c("error_", .)) %>%
      rename_all(~str_replace_all(., "flag", "")) %>%
      pivot_longer(-(id_cols), names_to=c('.value','item'), names_sep='_') %>%
      filter(!is.na(value) & value >= 0) %>%
      filter(!str_detect(error, 'A|K|L|H[|U]') | is.na(error)) %>%
      select(-error)

    Any column literally named "flag<x>" is paired with the value column "<x>".
    Columns with no matching flag column are still melted (error = NA).
    """
    id_cols = [c for c in id_cols if c in df.columns]
    flag_cols = {c: c[len("flag") :] for c in df.columns if c.startswith("flag") and c != "flagdepth"}
    value_cols = [c for c in df.columns if c not in id_cols and c not in flag_cols and f"flag{c}" != "flagdepth"]
    # value_cols should not themselves be flag columns
    value_cols = [c for c in value_cols if not c.startswith("flag")]

    records = []
    for item in value_cols:
        sub = df[id_cols + [item]].copy()
        sub = sub.rename(columns={item: "value"})
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


def clean_negatives(df: pd.DataFrame) -> pd.DataFrame:
    """mutate(across(everything(), ~replace(., . < 0, NA)))"""
    numeric_cols = df.select_dtypes(include="number").columns
    df = df.copy()
    for c in numeric_cols:
        df.loc[df[c] < 0, c] = pd.NA
    return df


# ---------------------------------------------------------------------------
# Per-dataset loaders (mirror the R functions of the same purpose)
# ---------------------------------------------------------------------------


def build_temp() -> pd.DataFrame:
    print("Loading physical limnology (temp/DO) - package 29 ...")
    raw = clean_negatives(load_latest_package("29"))
    if "flagdepth" in raw.columns:
        raw = raw.drop(columns=["flagdepth"])
    out = melt_value_flag_columns(raw, ID_CANDIDATES, exclude_bad_flags=True)
    return out


def build_secchi() -> pd.DataFrame:
    print("Loading secchi - package 31 ...")
    raw = clean_negatives(load_latest_package("31"))
    id_cols = [c for c in ["lakeid", "year4", "daynum", "sampledate", "sta"] if c in raw.columns]
    value_cols = [c for c in raw.columns if c not in id_cols]
    out = raw.melt(id_vars=id_cols, value_vars=value_cols, var_name="item", value_name="value")
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    out = out[out["value"].notna() & (out["value"] >= 0)]
    out["depth"] = 0
    out["rep"] = 1
    return out


def build_nutrients() -> pd.DataFrame:
    print("Loading nutrients - package 1 ...")
    raw = clean_negatives(load_latest_package("1"))
    raw = raw.rename(columns={c: c.replace("_WSLH", ".WSLH") for c in raw.columns if "_WSLH" in c})
    out = melt_value_flag_columns(raw, ID_CANDIDATES, exclude_bad_flags=True)
    is_wslh = out["item"].str.contains(r"\.WSLH", regex=True)
    out.loc[is_wslh, "value"] = out.loc[is_wslh, "value"] * 1000  # mg -> ug
    out["item"] = out["item"].str.replace(r"\.WSLH", "", regex=True)
    return out


def build_ions() -> pd.DataFrame:
    print("Loading major ions - package 2 ...")
    raw = clean_negatives(load_latest_package("2"))
    raw = raw.rename(
        columns={c: c.replace("_sloh", ".sloh").replace("_n", ".n") for c in raw.columns}
    )
    out = melt_value_flag_columns(raw, ID_CANDIDATES, exclude_bad_flags=True)
    return out

def build_ice() -> pd.DataFrame:
    print("Loading ice duration - packages 32 (north) + 33 (south) ...")
    north = load_latest_package("32")[["lakeid", "year", "duration"]]
    south = load_latest_package("33")[["lakeid", "year", "duration"]]
    south_nona = south.dropna(subset=['year']).copy()
    south_nona['year'] = south_nona['year'].astype(int)
    ice = pd.concat([north, south_nona], ignore_index=True)
    ice = ice.rename(columns={"year": "year4", "duration": "value"})
    ice["item"] = "iceduration"
    ice["sampledate"] = pd.to_datetime(ice["year4"].astype(str) + "-01-01")
    ice["daynum"] = ice["sampledate"].dt.dayofyear
    ice["depth"] = 0
    ice["rep"] = 1
    ice["sta"] = 1
    return ice[["lakeid", "year4", "daynum", "sampledate", "depth", "rep", "sta", "item", "value"]]


ZOOP_GROUP_MAP = {
    2: "cyclopoid",
    3: "calanoid",
    5: "cladocera",
    6: "rotifer",
}


def build_zoops() -> pd.DataFrame:
    print("Loading zooplankton - packages 37 (north) + 90 (south) ...")
    north = load_latest_package("37")
    south = load_latest_package("90")
    zoop = pd.concat([north, south], ignore_index=True)
    zoop["code"] = (zoop["species_code"] // 10000).astype(int)
    zoop = zoop[zoop["code"].isin(ZOOP_GROUP_MAP.keys())].copy()
    zoop["item"] = zoop["code"].map(ZOOP_GROUP_MAP)
    zoop = zoop.rename(columns={"sample_date": "sampledate"})
    zoop = (
        zoop.groupby(["lakeid", "year4", "sampledate", "item"], as_index=False)["density"]
        .sum()
        .rename(columns={"density": "value"})
    )
    zoop["sampledate"] = pd.to_datetime(zoop["sampledate"])
    zoop["daynum"] = zoop["sampledate"].dt.dayofyear
    zoop["depth"] = 0
    zoop["rep"] = 1
    zoop["sta"] = 1
    return zoop[["lakeid", "year4", "daynum", "sampledate", "depth", "rep", "sta", "item", "value"]]


# ---------------------------------------------------------------------------
# Static reference tables (mirror `matchtable` and `lakelocations` in server.R)
# ---------------------------------------------------------------------------

MATCHTABLE = [
    {"var": "wtemp", "name": "Water Temperature (\u00b0C)", "package": 29},
    {"var": "o2", "name": "Dissolved Oxygen (mg/L)", "package": 29},
    {"var": "o2sat", "name": "Dissolved Oxygen (% sat)", "package": 29},
    {"var": "doc", "name": "Dissolved Organic Carbon (mg/L)", "package": 1},
    {"var": "dic", "name": "Dissolved Inorganic Carbon (mg/L)", "package": 1},
    {"var": "toc", "name": "Total Organic Carbon (mg/L)", "package": 1},
    {"var": "tic", "name": "Total Inorganic Carbon (mg/L)", "package": 1},
    {"var": "no3no2", "name": "Nitrate + Nitrite as N (\u00b5g/L)", "package": 1},
    {"var": "nh4", "name": "Ammonium as N (\u00b5g/L)", "package": 1},
    {"var": "totnuf", "name": "Total Nitrogen unfiltered (\u00b5g/L)", "package": 1},
    {"var": "totnf", "name": "Total Nitrogen filtered (\u00b5g/L)", "package": 1},
    {"var": "drp", "name": "Dissolved Reactive Phosphorus (\u00b5g/L)", "package": 1},
    {"var": "totpuf", "name": "Total Phosphorus unfiltered (\u00b5g/L)", "package": 1},
    {"var": "totpf", "name": "Total Phosphorus filtered (\u00b5g/L)", "package": 1},
    {"var": "drsif", "name": "Dissolved Reactive Silica (\u00b5g/L)", "package": 1},
    {"var": "ph", "name": "pH", "package": 2},
    {"var": "alk", "name": "Alkalinity (ueq/L)", "package": 2},
    {"var": "ca", "name": "Calcium (mg/L)", "package": 2},
    {"var": "mg", "name": "Magnesium (mg/L)", "package": 2},
    {"var": "na", "name": "Sodium (mg/L)", "package": 2},
    {"var": "k", "name": "Potassium (mg/L)", "package": 2},
    {"var": "so4", "name": "Sulfate (mg/L)", "package": 2},
    {"var": "cl", "name": "Chloride (mg/L)", "package": 2},
    {"var": "cond", "name": "Specific Conductance (\u00b5S/cm)", "package": 2},
    {"var": "secview", "name": "Secchi with viewer", "package": 31},
    {"var": "secnview", "name": "Secchi without viewer", "package": 31},
    {"var": "iceduration", "name": "Lake ice duration (days)", "package": 32},
    {"var": "cladocera", "name": "Cladocera (#/L)", "package": 37},
    {"var": "calanoid", "name": "Calanoid copepod (#/L)", "package": 37},
    {"var": "cyclopoid", "name": "Cyclopoid copepod (#/L)", "package": 37},
    {"var": "rotifer", "name": "Rotifer (#/L)", "package": 37},
]
for row in MATCHTABLE:
    row["url"] = f"https://portal.edirepository.org/nis/mapbrowse?scope={SCOPE}&identifier={row['package']}"
    del row["package"]

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
]
LAKEID_TO_NAME = {row["lakeid"]: row["lake"] for row in LAKE_META}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    import os

    frames = []
    for label, builder in [
        ("temp", build_temp),
        ("secchi", build_secchi),
        ("nutrients", build_nutrients),
        ("ions", build_ions),
        ("ice", build_ice),
        ("zoops", build_zoops),
    ]:
        try:
            frames.append(builder())
        except Exception as exc:  # noqa: BLE001
            print(f"  !! failed to load {label}: {exc}", file=sys.stderr)

    if not frames:
        print("No datasets loaded successfully; aborting.", file=sys.stderr)
        sys.exit(1)

    all_lter = pd.concat(frames, ignore_index=True)
    all_lter["lakename"] = all_lter["lakeid"].map(LAKEID_TO_NAME)
    all_lter = all_lter[all_lter["lakename"].notna()]
    all_lter["sampledate"] = pd.to_datetime(all_lter["sampledate"]).dt.strftime("%Y-%m-%d")

    keep_cols = [c for c in ["lakeid", "lakename", "year4", "daynum", "sampledate", "depth", "rep", "item", "value"] if c in all_lter.columns]
    all_lter = all_lter[keep_cols]

    print(f"Combined dataset: {all_lter.shape[0]:,} rows")

    # Write one JSON file per variable so the browser only fetches what it needs.
    os.makedirs("data/vars", exist_ok=True)
    var_codes = all_lter["item"].unique()
    for var in var_codes:
        subset = all_lter[all_lter["item"] == var].drop(columns=["item"])
        out_path = f"data/vars/{var}.json"
        subset.to_json(out_path, orient="records")
        print(f"  wrote {out_path} ({subset.shape[0]:,} rows)")

    with open("data/matchtable.json", "w") as f:
        json.dump(MATCHTABLE, f, indent=2)
    with open("data/lakelocations.json", "w") as f:
        json.dump(LAKE_META, f, indent=2)
    with open("data/last_updated.json", "w") as f:
        json.dump({"updated": date.today().isoformat()}, f)

    print(f"Wrote {len(var_codes)} per-variable files to data/vars/, plus matchtable.json, lakelocations.json, last_updated.json")


if __name__ == "__main__":
    main()