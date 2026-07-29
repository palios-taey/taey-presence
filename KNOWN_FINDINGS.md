# Known Findings

## 2026-07-29 - Pre-deploy MAX_TOOL_ROUNDS Regression

Observed:
- Live production `/home/mira/taey-presence-validate/soma_proxy_mira.py` defaulted `MAX_TOOL_ROUNDS` to 60.
- Candidate commit `f01ee6f` defaulted `serving/soma_proxy.py` `MAX_TOOL_ROUNDS` to 8.
- The inspected systemd environment had no `MAX_TOOL_ROUNDS` override, so deploying the candidate as-is would have used the lower default.
- Production was not restarted during this finding.

Impact:
- The candidate would have regressed long tool workflows by forcing final prose after 8 rounds instead of preserving the live 60-round ceiling.

Corrective action:
- Candidate `serving/soma_proxy.py` now preserves the live default of 60 while retaining the `MAX_TOOL_ROUNDS` environment override.
