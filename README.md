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

- **Check inputs with InVEST before running** (default on) validates parameters
  with InVEST itself before the model starts. See below.
- **Load intermediate outputs onto the map by default** sets the initial state
  of the per-run checkbox described below.

## Using it

Models are grouped in the toolbox under *InVEST* (Freshwater, Marine and
Coastal, Terrestrial, Urban, Support Tools). Pick a model, fill in the
parameters, and choose a workspace folder for the results.

### Loading parameters from a datastack

Rather than filling in a dozen paths by hand, press **Load Parameters…** at the
bottom of any InVEST algorithm dialog and pick an InVEST datastack
(`.invest.json`, or the older `.invs.json`). The inputs are populated in place,
so you can review and adjust them before running.

Datastacks in the wild are messy, and the loader handles that rather than
failing: relative paths are resolved against the datastack file, quoted numbers
(`"0.8"`) are converted, `"true"`/`"false"` become real booleans, and dropdown
values are matched case-insensitively.

Anything that does not carry over is reported instead of being silently
dropped — which matters, because InVEST renames arguments between releases. The
Carbon datastack in the 3.13 sample data is from InVEST 3.7 and still says
`lulc_cur_path` where the current model says `lulc_bas_path`; loading it fills
in what it can and tells you the rest. The full breakdown goes to the *InVEST*
tab of the Log Messages panel.

When the run finishes, the model's spatial results are added to the map in a
layer-tree group named after the model.

InVEST organises a workspace one directory deep, and the distinction that
matters is *which* directory: `output/`, `outputs/` and `visualization_outputs/`
hold results and are loaded, while `intermediate/`, `intermediate_outputs/` and
`tmp/` hold working files and are not. The advanced parameter **Also load
intermediate outputs onto the map** loads those too; InVEST writes them either
way. The `taskgraph_cache` directory is never loaded.

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
  optional widgets labelled with their condition, and the rule is enforced by
  InVEST's validator before the run starts.
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

### Validation

Inputs are checked by InVEST's own validator, so the dialog enforces exactly
what the model enforces.

Checking happens **as you type**. Shortly after each edit the form is re-checked
and two things update:

- Inputs with a problem have their label turned red, with InVEST's message as
  the tooltip.
- Inputs that do not currently apply are greyed out.

The greying out is InVEST's `args_enabled` rule, and it is what makes the
model's *conditional* structure visible. Tick **Calculate sequestration** on the
Carbon model and Alternate LULC becomes active; tick **Run valuation** and the
price, discount rate and year fields join it. The Processing dialog has no way
to express those dependencies itself, so without this they would all look
equally required.

An input that is greyed out is never flagged as a problem, since it is not
something you can act on.

Press **Validate** for a summary at any point, and validation runs again when
you press Run — an invalid run is blocked with the problems listed against the
input labels you see in the form, rather than failing a minute into execution.

Validation is served by a background InVEST process that starts when you open
an InVEST dialog. It takes about a minute to come up, after which each check
takes a few milliseconds — cheap enough to run on every edit. Checks are
debounced by half a second, and layer inputs are read by their existing path
rather than being exported, so typing never triggers a file conversion.

Until the server is ready there is no as-you-type feedback, and pressing Run
skips validation rather than stalling QGIS. The dialog watches for the server
finishing and shows the current state as soon as it can, so a form filled in
during that first minute does not sit there looking unchecked. Pressing
**Validate** before it is warm starts it in the background and reports when
finished. The process is shut down when the plugin is unloaded.

The workspace folder is deliberately left out of loaded datastacks even when
one names it, so a saved parameter set cannot quietly overwrite a previous run's
results — pick the workspace yourself.

## Tests

Offline tests cover the spec translation for every model in two InVEST
releases, and need neither QGIS nor InVEST:

```bash
python3 -m unittest discover -s tests -v
```

The fixtures in `tests/` are real `getspec` output harvested from InVEST 3.16.2
and 3.20.0.

## Limitations

- Result and working directories are recognised by name. A future InVEST
  release introducing a new working-directory name would have its contents
  loaded as results until that name is added to `_INTERMEDIATE_DIRS` in
  `normalize.py`. The tests assert that every model contributes at least one
  layer by default, which catches the more damaging direction of this mistake.
- Only the macOS Workbench layout has been verified. The Windows and Linux
  executable locations are best-effort guesses.
- `coastal_vulnerability`'s `slr_field` is a dropdown InVEST computes at run
  time from another input, so it is presented as a free-text field.
- The InVEST 3.20 Carbon spec does not declare its HTML report, so that file is
  written to the workspace but not listed as a formal output.
- Validation needs the InVEST server to be warm. For roughly the first minute
  of a session, pressing Run skips validation rather than stalling QGIS, so an
  invalid run can still start during that window and fail the way it would
  have before. Pressing **Validate** waits for the server instead of skipping.
- Live checking stays silent until the InVEST server is warm, so for roughly
  the first minute of a session there is no as-you-type feedback and no
  greying out. The dialog refreshes itself once the server is up.
