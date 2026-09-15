#!/usr/bin/env bash
# Independent staged primitive. Not called by a maintained runner or normal init.
# Requires a locally installed candidate and reviewed broker policy/authority/
# actual transport inputs. Fresh exact target/branch/labels and bounded public
# history are read by the real API before an attribution artifact is written.
# Publication and label reconciliation remain separate explicit Python calls.
set -euo pipefail
python - <<'PY'
import os
from code_mower.builder_lineage_producer import staged_record
staged_record(os.environ)
PY
