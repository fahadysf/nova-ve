"""EVE-NG template parsers (#186).

Three parsers feed the adapter registry with a normalised intermediate dict:

- :func:`yaml_parser.parse_yaml` for modern EVE-NG ``html/templates/intel/<vendor>.yml``.
- :func:`php_parser.parse_php` for legacy EVE-NG ``html/templates/<vendor>.php``.
- :func:`wrapper_parser.parse_wrapper` for qemu_wrappers/``<vendor>_wrapper.py``.

All three return a ``dict`` whose schema is the union of fields declared in
the GH #186 body; unknown content is preserved under ``_eveng_raw`` so the
importer never silently drops information it does not understand.
"""

from .php_parser import parse_php, ParserError
from .wrapper_parser import parse_wrapper
from .yaml_parser import parse_yaml

__all__ = ["parse_yaml", "parse_php", "parse_wrapper", "ParserError"]
