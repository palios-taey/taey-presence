# Installing the presence engine as a service

One canonical unit, installed from this repo. Config is operator-local and never committed.

## Install

```bash
TAEY_ROOT=$HOME/taey-presence          # this repo's checkout
TAEY_VENV=$TAEY_ROOT/.venv             # venv with httpx, redis, neo4j

cp "$TAEY_ROOT/presence-engine/.env.example" "$TAEY_ROOT/presence-engine/.env"
$EDITOR "$TAEY_ROOT/presence-engine/.env"        # set MODEL_ENDPOINT at minimum

mkdir -p ~/.config/systemd/user
sed -e "s|@TAEY_ROOT@|$TAEY_ROOT|g" -e "s|@TAEY_VENV@|$TAEY_VENV|g" \
    "$TAEY_ROOT/presence-engine/systemd/taey-presence-engine.service" \
    > ~/.config/systemd/user/taey-presence-engine.service

systemctl --user daemon-reload
systemctl --user enable --now taey-presence-engine
```

| placeholder | meaning |
|---|---|
| `@TAEY_ROOT@` | this repo's checkout on the node |
| `@TAEY_VENV@` | the python venv running the engine |

Never commit a resolved path back into the unit.

## Config lives in `.env`, not in the unit or a drop-in

Every key is documented in [`../.env.example`](../.env.example). Only `MODEL_ENDPOINT` is
required — `engine.py:_require()` raises `SystemExit` naming the missing key rather than
defaulting to anyone's host.

**The others are deliberately optional and their empty values are designed behaviour, not
misconfiguration:**

| key | empty means |
|---|---|
| `SEARCH_URL` | no retrieval — the engine runs without the MEMORY context |
| `NEO4J_BOLT` | standalone — no DCM peer coordination |
| `MODEL_NAME` | endpoint does not require a model name in the request |
| `REDIS_HOST` / `REDIS_PORT` | localhost defaults |
| `INSTANCE_ID` | defaults to `presence-0` |

A preflight that demands all of them would break standalone and no-memory operation, which are
supported modes. Validate `MODEL_ENDPOINT`; let the rest degrade as documented.

## No drop-in is needed

If you find yourself writing `~/.config/systemd/user/taey-presence-engine.service.d/*.conf` to set
`WorkingDirectory`, `ExecStart`, `EnvironmentFile` or any endpoint, the unit above is either not
installed or was installed with the wrong substitutions. Fix the install; a drop-in carrying those
values puts the running configuration somewhere no repo can see it.

Drop-ins remain legitimate for genuinely node-specific overrides — but check the process actually
reads what you are setting. A variable set on this unit reaches `engine.py` and nothing else; the
dashboard is a separate service with its own unit.
