"""Schema round-trip tests for design.yaml."""
from pathlib import Path

import pytest
import yaml

pydantic = pytest.importorskip("pydantic")

from standl.schema import Design


EXAMPLE = Path(__file__).parent.parent / "examples" / "design.example.yaml"


def test_example_loads():
    data = yaml.safe_load(EXAMPLE.read_text())
    design = Design.model_validate(data)
    assert design.dataset_id == "GSE139324"
    assert len(design.samples) == 2
    assert design.samples[0].donor_id == "HN01"


def test_contrast_references_valid_factor():
    data = yaml.safe_load(EXAMPLE.read_text())
    design = Design.model_validate(data)
    factor_names = {f.name for f in design.factors}
    for c in design.contrasts:
        for d in (c.numerator, c.denominator):
            for k in d:
                assert k in factor_names, f"contrast {c.name} references unknown factor {k}"
