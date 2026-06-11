# Manual overrides

The **only** place for hand corrections. Never edit snapshots or the DB.

One YAML file per fix, applied as the last pipeline step, so fixes survive
every automated rebuild and the diff report shows when upstream catches up.

Format (`2026-06-10-example.yaml`):

```yaml
reason: "Phone number on justizadressen is stale; confirmed by calling"
source: "https://www.amtsgericht-X.de/kontakt"
date: 2026-06-10
expires: 2026-12-31        # re-check by this date; pipeline warns when past
match:                     # how to find the record (external id preferred)
  external_id: { scheme: xjustiz, value: D2101 }
set:
  phone: "+49 8251 ..."
```
