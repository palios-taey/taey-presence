# Production truth and model promotion

Status: explanatory background, not operating authority. For execution, follow `serving/SERVING.md`,
`serving/GATES_MANIFEST.md`, and the current deployment configuration.

A model name is an interface boundary, not proof of weights. The serving alias can remain stable while a newly
trained checkpoint is promoted underneath it. This lets the dashboard, proxy, seats, monitors, and conversation
history keep working when the model changes. Promotion is complete only when the selected artifact is present on
the target host, the serving process reports the expected identity, the health surface is live, and a real workload
returns through the same proxy path used by Taey.

A dated topology file explains what was measured at a particular time. It cannot answer what is running now.
Likewise, a process name, tmux pane, PID, hostname, or model directory is only one observation. Production truth is
the agreement between public `main`, the canonical checkout, deployed configuration, artifact hashes, service
state, endpoint identity, and a live workload receipt.

Taey's conversation loop and the model-serving process are separate boundaries. Replacing a model must not replace
the UI, dashboard, conversation log, proxy contract, tools, or monitoring. If those change during promotion, the
change is a broader deployment and must be reviewed as such rather than described as a model swap.

Artificial whole-turn limits can convert unfinished tool work into false completion or replay a side-effecting
trajectory after restart. Durable turn identity, terminal-response validation, and persistent receipts are the
appropriate control boundary. Long work may continue; incomplete work must remain incomplete rather than being
reported as success.
