# NTL-LTER Data Viewer

An interactive viewer for long-term data from the [North Temperate Lakes Long-Term Ecological Research](https://lter.limnology.wisc.edu/) (NTL-LTER) program, based at the University of Wisconsin–Madison Center for Limnology.

Explore four decades of measurements from 13 Wisconsin lakes: the seven Trout Lake area lakes in northern Wisconsin, the four Madison-area lakes in southern Wisconsin, and Lakes Kegonsa and Waubesa. Data are refreshed weekly from the [Environmental Data Initiative (EDI)](https://edirepository.org/) repository.

## Lakes

| Northern (Trout Lake area) | Southern (Madison area) |
|---|---|
| Allequash Lake, Big Muskellunge Lake, Crystal Bog, Crystal Lake, Sparkling Lake, Trout Bog, Trout Lake | Lake Mendota, Lake Monona, Lake Wingra, Fish Lake, Lake Kegonsa, Lake Waubesa |

## What you can explore

- **Physical:** water temperature, dissolved oxygen, lake levels
- **Ice & snow:** ice duration, ice-on and ice-off dates (Lake Mendota records back to 1853), ice thickness, snow depth
- **Light & clarity:** Secchi depth, light extinction, underwater and surface light
- **Nutrients & carbon:** nitrogen, phosphorus, silica, and organic and inorganic carbon
- **Ions:** pH, alkalinity, major ions, iron, manganese, conductivity
- **Algae:** chlorophyll, phaeophytin, and phytoplankton biomass by group
- **Zooplankton:** density and biomass of cladocerans, copepods, and rotifers
- **Macroinvertebrates:** pelagic and benthic invertebrates, crayfish
- **Macrophytes:** species frequency and biomass
- **Fish:** catch per unit effort, species richness, lengths, weights, and length-frequency distributions

Not every variable is measured in every lake. If a selection has no data, the viewer tells you which lakes do.

## Using the viewer

1. **Choose a variable** from the grouped menu.
2. **Choose one or more lakes**, or select all northern or all southern lakes at once.
3. Depending on the variable, **choose a depth, species, gear type, or year**.
4. For depth-sampled variables, switch between **time series**, **annual means**, and **monthly boxplots**.
5. Turn on a **log y-axis** if values span a wide range.
6. Click and drag on the plot to zoom (double-click to reset). Use the camera icon to download the plot as a PNG.
7. Click **Show map of sites** to see the lakes, with basic lake facts.

## Notes on the data

- Values flagged as suspect by NTL-LTER, and a small number of physically impossible values (for example, water temperatures above 40 °C), are left out of the plots.
- Depths listed as a range (for example "0–2 (integrated)") are samples taken over that depth range rather than at a single depth.
- Zooplankton biomass is calculated from densities and NTL-LTER length–mass formulas. A few taxa without a formula, including the spiny water flea (*Bythotrephes*), are not included in biomass.
- The viewer is for exploring data. For analysis, download the original datasets from EDI.
- Questions about the data, including potential data issues, should be directed to the [NTL Data Manager](mailto:lter.im@limnology.wisc.edu).

## Citing the data

Please cite the original datasets. The footer of the viewer links to the EDI page for each variable you are viewing, where you will find the full citation and DOI. The full list of NTL-LTER core datasets is at [lter.limnology.wisc.edu/core-datasets](https://lter.limnology.wisc.edu/core-datasets/).

## Acknowledgments

This material is based upon work supported by the National Science Foundation under Cooperative Agreement #DEB-2025982, NTL LTER. Any opinions, findings, conclusions, or recommendations expressed in the material are those of the author(s) and do not necessarily reflect the views of the National Science Foundation.
