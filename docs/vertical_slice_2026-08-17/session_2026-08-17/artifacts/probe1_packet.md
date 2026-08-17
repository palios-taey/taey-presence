# taey-delegate collect PRODUCTION PROBE

Infra asked for this. The tool merged, was installed, and needs a real production run.

## The procedure I followed

1. Wrote these two files to disk under /home/mira/taey_runs/
2. Ran: `taey-delegate collect <packet.md> <packet.md> -o <manifest.json>`
3. Checked exit code and output
4. Sent the packet plus manifest to Gaia (:3) via the receipted relay:
   - `extract` with `output_file` to get a path + SHA-256 receipt
   - `paste` with `text_file` to deliver by path
5. Report back what ran, the manifest path, and what Gaia says

## What to check

- Did `collect` exit 0?
- Did it refuse anything (missing file, hash mismatch)?
- Does the manifest contain exactly two entries?
- Did the receipted relay deliver the packet by PATH without me reading the body?

## Constraint

Do not report a hash, byte count, or path that you did not get from the tool or from a tool receipt. If you did not check something, say [Unknown]. That is the point of this tool.

## Ground truth

These are the only files and paths in this run:
- /home/mira/taey_runs/probe1_packet.md
- /home/mira/taey_runs/probe1_manifest.json

Anything else is a bug in the tool, not in this procedure.
