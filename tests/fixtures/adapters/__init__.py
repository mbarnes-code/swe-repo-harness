"""Fixture-only, genuinely new-language adapter pair for SPEC §12.34 Clause A.

Nothing here is a real language integration — `tests/test_new_language_touchpoints_e2e.py` is the
only consumer, and it proves SPEC §1's "a new language costs exactly the documented touchpoints"
claim by adding a fixture "Ruby" `ManifestAdapter` + `EcosystemAdapter` pair with **zero**
`src/fleet/` changes.
"""

from __future__ import annotations
