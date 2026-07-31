# Regular-package marker. Without it this directory was a NAMESPACE package, and Python gives a
# regular package (infra-soul/dashboard, which has __init__.py) precedence over a namespace package
# REGARDLESS of sys.path order. So `dashboard.app` resolved to infra-soul's May-27 dashboard even
# with this directory first in PYTHONPATH and set as WorkingDirectory — which is why the operator's
# UI "reverted to an extremely old version" and why running uvicorn by hand (no PYTHONPATH) worked.
