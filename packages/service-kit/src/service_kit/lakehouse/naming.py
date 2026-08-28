"""Catalog table-id naming — the ONE delimiter the estate's governed table ids use."""

from __future__ import annotations


#: The catalog table-id delimiter (``gold$catalog``, ``silver$features``). ONE definition for the whole
#: estate: every config knob defaults to it and every parse site splits on it, so the ids the catalog
#: mints and the ids every producer / mover / reader parses cannot drift apart. The Lance Namespace
#: layout and the medallion's ``<stage>$<table>`` convention both fix this at ``$``; a service's env
#: override (``LANCE_NS_DELIMITER`` and its siblings) exists so an operator who changes it changes it
#: from this single default — never so one plane can diverge from another.
CATALOG_DELIMITER = "$"
