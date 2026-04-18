"""Direct merge() tests. The merger is exercised indirectly by meta_check
and run tests; these cover behaviors without the rest of the pipeline in
the loop."""
from __future__ import annotations

from standl.merge import merge
from standl.schema import PartialDesign, ProvenancedValue


def test_notes_survive_merge_concatenated_in_order():
    """Both partial.notes values must appear in the merged Design.notes.
    The skill's pooled-series rescue flow reads these, so dropping them on
    the floor would strand users."""
    p1 = PartialDesign(
        extractor="geo-soft",
        dataset_id="GSE9",
        organism=ProvenancedValue(value="H", source="geo-soft"),
        assay=ProvenancedValue(value="10x", source="geo-soft"),
        notes="series_supplementary_files: ftp://host/a.gz; ftp://host/b.gz",
    )
    p2 = PartialDesign(extractor="manual", notes="hand-edit: corrected condition labels")

    design, _ = merge([p1, p2])
    assert design.notes is not None
    assert "ftp://host/a.gz" in design.notes
    assert "hand-edit" in design.notes


def test_notes_none_when_no_partial_has_notes():
    p = PartialDesign(extractor="geo-soft", dataset_id="GSE0")
    design, _ = merge([p])
    assert design.notes is None
