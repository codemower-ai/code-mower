#!/usr/bin/env bash
# Materialized attribution entrypoint. Publication belongs to the supervised runner.
# Requires capable installed APIs and reviewed broker policy/authority/
# actual transport inputs. Fresh exact target/branch/labels and bounded public
# history are read by the real API before an attribution artifact is written.
# Publication and label reconciliation remain separate explicit Python calls.
set -euo pipefail
python - <<'PY'
import os
try:
    from code_mower.provider_runners.lineage import installed_record
except ImportError:
    raise SystemExit("Unsupported installed lineage capability; release activation requires #915")
installed_record(os.environ)
PY
