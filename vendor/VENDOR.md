# Vendored: lerobot's motor layer

## Why vendoring is necessary

Line 36 of `src/lerobot/utils/__init__.py`:

```python
from .device_utils import auto_select_torch_device, get_safe_torch_device, is_torch_device_available
```

and line 20 of `device_utils.py` is an unguarded `import torch`. Because Python
executes a package's `__init__.py` before any of its submodules, **importing any
lerobot hardware module pulls torch in** — even when all you want is to read a
serial port.

Measured by blocking torch with a `MetaPathFinder` and then importing:

```
lerobot/motors/motors_bus.py:37   from lerobot.utils.import_utils import ...
  -> lerobot/utils/__init__.py:36  from .device_utils import ...
    -> lerobot/utils/device_utils.py:20   import torch      <-- ImportError
```

The console has no use for torch, so these files are copied here and the
`lerobot.utils.*` imports are retargeted at `_compat`.

## Provenance

| | |
|---|---|
| Upstream commit | `eb7a54c` |
| Copied on | 2026-10-15 |

## File inventory and changes

| File here | Upstream path | Change |
|---|---|---|
| `motors_bus.py` | `src/lerobot/motors/motors_bus.py` | Two import lines retargeted (`:37`, `:49-50`) |
| `encoding_utils.py` | `src/lerobot/motors/encoding_utils.py` | **None** — verbatim |
| `feetech/feetech.py` | `src/lerobot/motors/feetech/feetech.py` | One import line retargeted (`:21`) |
| `feetech/tables.py` | `src/lerobot/motors/feetech/tables.py` | **None** — verbatim |
| `feetech/__init__.py` | `src/lerobot/motors/feetech/__init__.py` | **None** — verbatim |
| `errors.py` | `src/lerobot/utils/errors.py` | **None** — verbatim |
| `decorators.py` | `src/lerobot/utils/decorators.py` | **None** — verbatim |
| `_compat.py` | (new) | Stands in for four symbols from `lerobot.utils.import_utils` + `lerobot.utils.utils` |
| `__init__.py` | (new) | Stands in for `src/lerobot/motors/__init__.py` |

### The three import patches, exactly

```diff
# motors_bus.py
-from lerobot.utils.import_utils import _deepdiff_available, _serial_available, require_package
+from ._compat import _deepdiff_available, _serial_available, require_package
 ...
-from lerobot.utils.decorators import check_if_already_connected, check_if_not_connected
-from lerobot.utils.utils import enter_pressed, move_cursor_up
+from ._compat import enter_pressed, move_cursor_up
+from .decorators import check_if_already_connected, check_if_not_connected

# feetech/feetech.py
-from lerobot.utils.import_utils import _feetech_sdk_available, require_package
+from .._compat import _feetech_sdk_available, require_package
```

The `from ..encoding_utils import ...`, `from ..motors_bus import ...` and
`from .tables import ...` lines inside `feetech.py` need **no change at all** —
this directory deliberately preserves upstream's relative layout.

## Re-syncing

After upstream changes the motor layer:

```bash
tools/debug_console/.venv/bin/python tools/debug_console/vendor/sync_vendor.py
```

The script re-copies, re-applies the three patches above, and runs the "no torch"
check. It diffs against the upstream sources, so a patch that no longer applies
fails loudly instead of quietly producing a broken file.

## Known differences from upstream

- `record_ranges_of_motion()` is kept verbatim (it still polls stdin). **The console
  never calls it** — there is no stdin to press Enter on in a browser, and the
  `msvcrt.kbhit()` inside `enter_pressed()` raises `OSError` when stdin is not a
  console. Sweeps are driven by `SweepRecorder` in `app/calibration.py` instead.
- `_compat.require_package` reports this tool's install command; behaviour is
  otherwise identical.
