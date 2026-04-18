#!/usr/bin/env bash
# End-to-end demo: standl run + hand-edit + validate on GSE96583 (Kang 2018).
#
# Not covered here: stanobj / stangene / downstream. This script's
# authoritative output is ../examples/gse96583/{design,manifest,provenance}.{yaml,json}
# + audit.md. If you re-run and the artifacts drift, commit the diff.

set -euo pipefail

OUT=${1:-/tmp/gse96583}
HAND_EDITED_DESIGN="$(dirname "$0")/../examples/gse96583/design.yaml"

rm -rf "$OUT"

# 1. geo-soft → fetch → validate (produces an initial design with NO factors/contrasts).
standl run GSE96583 -o "$OUT"

# 2. Replace the auto-filled design with the hand-edited one that adds factors,
#    contrasts, condition, donor_id, batch. See skills/standl/SKILL.md for the
#    human-in-the-loop playbook.
cp "$HAND_EDITED_DESIGN" "$OUT/design.yaml"

# 3. Revalidate — worst severity should be `warn` (condition ↔ donor_id confound,
#    expected for this library-level pooled design; see examples/gse96583/README.md).
standl validate "$OUT"

grep -E '^Worst severity' "$OUT/audit.md"
