# InVEST for QGIS

Runs [InVEST](https://naturalcapitalproject.stanford.edu/software/invest)
ecosystem-service models from the QGIS Processing Toolbox. Every InVEST model
appears as a Processing algorithm, inputs are entered with normal QGIS widgets
(so layer pickers see the layers already in your project), and spatial outputs
are added to the map when the run finishes.

The plugin does not bundle InVEST. It drives the `invest` executable inside an
InVEST Workbench application that you install separately.

## Requirements

- QGIS 3.34 or newer (tested on 3.44.12; written to also work on QGIS 4)
- An installed InVEST Workbench application (tested with InVEST 3.16.2 and 3.20.0)

## Install

Copy or symlink the `invest_qgis` directory into your QGIS plugin folder:

```bash
ln -s "$PWD/invest_qgis" "$HOME/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/invest_qgis"
```

On Windows that folder is
`%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins`, and on Linux
`~/.local/share/QGIS/QGIS3/profiles/default/python/plugins`.

Then enable **InVEST** in *Plugins ▸ Manage and Install Plugins*.

## Configure

Open *Processing ▸ Options ▸ Providers ▸ InVEST* and set **InVEST application**
to the Workbench you installed, for example
`/Applications/InVEST 3.20.0 Workbench.app`. The plugin finds the executable
inside the bundle itself.

The first time you set this, the plugin reads the specification of every InVEST
model in the background. This takes about a minute, after which the models
appear in the Processing Toolbox. The result is cached, so later QGIS sessions
start instantly. Use *Plugins ▸ InVEST ▸ Refresh InVEST models* to re-read them
after upgrading InVEST.

Two other settings are available:

- **Validate inputs before running** (default off) runs InVEST's own validator
  before the model. It catches problems early but adds roughly a minute.
- **Load intermediate outputs onto the map by default** sets the initial state
  of the per-run checkbox described below.

## Using it

Models are grouped in the toolbox under *InVEST* (Freshwater, Marine and
Coastal, Terrestrial, Urban, Support Tools). Pick a model, fill in the
parameters, and choose a workspace folder for the results.

When the run finishes, top-level spatial outputs are added to the map in a
layer-tree group named after the model. The advanced parameter **Also load
intermediate outputs onto the map** additionally loads the contents of the
model's `intermediate_outputs` directory; InVEST writes those files either way.

Non-spatial outputs (summary tables, HTML reports, the run log) are left in the
workspace folder.

## How it works, and why

InVEST cannot be imported into QGIS's bundled Python — it needs its own GDAL,
`pygeoprocessing` and `taskgraph` stack. The plugin therefore runs models out of
process through the frozen `invest` executable, streaming its log into the
Processing feedback panel.

That frozen executable takes roughly a minute to start, every time. Asking it
for each model's specification separately would take about 25 minutes for the
full set, so the plugin instead starts `invest serve` once and reads all the
specs over its local HTTP API in well under a second. This is why there is a
one-off background harvest and an on-disk cache rather than a per-model query.

The same startup cost applies to each model run. It is minor next to a model
that runs for minutes, but it does mean a fast model like Carbon spends most of
its wall-clock time starting up. The progress text says so rather than letting
the dialog look hung.

Algorithms are generated from the model specs rather than hand-written, so a
new InVEST release is picked up by refreshing the model list. The plugin
normalises two different spec layouts (InVEST 3.16 nests outputs under
directory entries; 3.20 gives each an explicit path) into one internal shape.

### Notable behaviours

- **Conditional inputs.** InVEST expresses some requirements as an expression
  over other inputs (`lulc_alt_path` is required only if `calc_sequestration`
  is checked). The Processing dialog cannot evaluate those, so such inputs are
  optional widgets labelled with their condition, and InVEST enforces the real
  rule when the model runs.
- **Vector inputs** are passed through
  `parameterAsCompatibleSourceLayerPathAndLayerName`, so memory layers, a
  "Selected features only" choice, and provider subset filters are all honoured
  rather than silently passing InVEST the full dataset.
- **Raster inputs** must be file-based (GDAL). A WMS or in-memory raster is
  rejected with a clear message instead of failing deep inside the model.
- **`n_workers` is not exposed.** Models always run synchronously, because
  spawning taskgraph worker processes from a subprocess of QGIS is unreliable
  across platforms.
- **Cancelling stops the whole InVEST process tree.** InVEST is started in its
  own process group so that pressing Cancel terminates it and any helper
  processes it started, rather than orphaning a model that keeps writing to the
  workspace.

## Tests

Offline tests cover the spec translation for every model in two InVEST
releases, and need neither QGIS nor InVEST:

```bash
python3 -m unittest discover -s tests -v
```

The fixtures in `tests/` are real `getspec` output harvested from InVEST 3.16.2
and 3.20.0.

## Limitations

- Only the macOS Workbench layout has been verified. The Windows and Linux
  executable locations are best-effort guesses.
- `coastal_vulnerability`'s `slr_field` is a dropdown InVEST computes at run
  time from another input, so it is presented as a free-text field.
- The InVEST 3.20 Carbon spec does not declare its HTML report, so that file is
  written to the workspace but not listed as a formal output.
