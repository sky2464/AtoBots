#!/usr/bin/env bash
# The second half of an update. The first half (engine/update.sh or
# engine/adopt.sh of the version that was installed) backs up, holds the
# queue, stops the dispatcher and switches the code. Then it runs THIS file
# from the new tree, so each version finishes its own update: schema,
# migrations, units, restart, verification and, on failure, the rollback.
#
# The contract with the first half is the CB_UPD_* environment variables
# (see update_finish.py). This file's path must never change.
set -euo pipefail
ENGINE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
exec "$ENGINE_DIR/bin/cb" update-finish "$@"
