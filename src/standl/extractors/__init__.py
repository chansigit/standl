"""Pluggable design extractors.

Add a new source (HCA DCP, Zenodo, CELLxGENE, ...) by creating a module here
that subclasses ``DesignExtractor`` and registers itself via ``@register``.
The core never hardcodes which extractor runs — it asks each one
``can_handle(source) -> float`` and runs every extractor above threshold,
then lets the merger reconcile the outputs.
"""
from .base import DesignExtractor, register, all_extractors, pick_extractors

# Import for side-effect (registration). New extractors: add import here.
from . import biostudies  # noqa: F401
from . import cellxgene_api  # noqa: F401
from . import figshare  # noqa: F401
from . import geo_soft  # noqa: F401
from . import gsa_cncb  # noqa: F401
from . import h5ad_observed  # noqa: F401
from . import hca_dcp  # noqa: F401
from . import sciencedb_cn  # noqa: F401
from . import zenodo  # noqa: F401

__all__ = ["DesignExtractor", "register", "all_extractors", "pick_extractors"]
