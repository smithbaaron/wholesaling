"""External service integrations.

Every integration is a subpackage with three modules (REQUIREMENTS.md XC-11, RUL-02):

- ``interface.py``  a ``Protocol`` or ABC describing the operations the system needs
- ``mock.py``       an in-memory implementation used by tests and the synthetic pilot
- ``real.py``       the live implementation, constructed only when ``<SERVICE>_MODE=live``

``factory`` functions pick the implementation from settings. Nothing outside ``integrations``
imports a ``real`` module directly.
"""
