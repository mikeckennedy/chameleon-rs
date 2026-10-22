## shadow.install()


Starts shadow mode: from now on, a `sample_rate` share of the outermost renders of


Usage

``` python
shadow.install(
    *, serve="chameleon", sample_rate=1.0, report=None, log_interval=600.0
)
```


stock Chameleon page templates also renders with chameleon-rs (doubling their latency: keep it well below 1 on real traffic), and the application gets the outcome (output or exception) of the `serve` engine. Differences go to `report` (default: a capped warning on the `chameleon_rs.shadow` logger, once per distinct difference per `log_interval` seconds). Calling it again replaces the settings.
