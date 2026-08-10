# NTL-LTER Data Viewer

An interactive web visualization tool for North Temperate Lakes Long-Term Ecological Research ([NTL-LTER](https://lter.limnology.wisc.edu/)) data.

Explore physical, chemical, and biological parameters across 11 Wisconsin lakes from 1981 to present, including water temperature, nutrients, ions, Secchi depth, ice duration, zooplankton, and fish/crayfish catch per unit effort (CPUE).

## Features

- **Interactive Plots**: View time-series, annual means, and monthly boxplots powered by Plotly.js.
- **Lake Map**: See site locations using Leaflet.js.
- **Data Customization**: Filter by lake groups (Northern & Southern sites), depth, species, and sampling gear.
- **Automated Data Refresh**: Python workflow fetches and processes updated datasets weekly from the Environmental Data Initiative (EDI) PASTA REST API.

## App Structure

- `index.html` - Main Web Application UI.
- `css/` - Custom styles and UI design.
- `js/` - Application logic (`app.js`).
- `data/` - Processed JSON datasets (`all_lter.json`, `matchtable.json`, `lakelocations.json`).
- `fetch_data.py` - Python script to fetch, clean, and convert raw LTER datasets from EDI into JSON.
- `.github/workflows/` - CI/CD pipeline for weekly data updates.
