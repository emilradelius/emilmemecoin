# Running the paper phase

This is the rehearsal. No broker, no account, no money. The point is to find
out whether the machinery survives weeks of running unattended - not whether
the strategy makes money, which it is measured for separately and, on the
evidence so far, does not.

## Start it

```bash
cd ~/Desktop/emilmemecoin
.venv/bin/python -m brokerbot.cli preflight --broker paper --symbols VOLV-B.ST,ERIC-B.ST,INVE-B.ST
```

Preflight must be clean before you start. It checks the things that waste a
week when they are wrong: that every symbol has a price, that they are all in
the account's currency, and that the broker answers at all.

Then:

```bash
.venv/bin/python -m brokerbot.cli trial --broker paper \
    --symbols VOLV-B.ST,ERIC-B.ST,INVE-B.ST \
    --strategy momentum --days 42 --cycle-seconds 900
```

It polls every 15 minutes and logs orders without sending them. Leave the
terminal open, or install the background job below.

## Check on it

```bash
.venv/bin/python -m brokerbot.cli trial --report
```

Read the operational lines: uptime, failed cycles, position drift, missing
prices. Those are what this phase is for. Ignore the profit number - six weeks
of daily bars is far too little to mean anything, and the report says so
itself.

## Stop it

```bash
touch data/live/STOP
```

The runner notices within one cycle and shuts down cleanly. This works whether
it is running in a terminal or in the background.

## Running it in the background

`launchd/com.emilradelius.brokerbot.plist` runs the trial across reboots and
logouts. Install it with:

```bash
cp launchd/com.emilradelius.brokerbot.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.emilradelius.brokerbot.plist
```

To stop it permanently:

```bash
launchctl unload ~/Library/LaunchAgents/com.emilradelius.brokerbot.plist && rm ~/Library/LaunchAgents/com.emilradelius.brokerbot.plist
```

Output goes to `data/live/trial.log`. Edit the symbols and strategy in the
plist before installing - it is a plain text file and the arguments are listed
one per line.

## Refreshing the price history

`data/history/*.csv` holds ten years of split- and dividend-adjusted daily
bars, exported so backtests stay reproducible. Yahoo revises history, so a
backtest keyed to the live feed quietly changes underneath you; one keyed to
these files does not. Re-export when you want newer data:

```bash
.venv/bin/python -m brokerbot.cli history --symbols VOLV-B.ST,ERIC-B.ST --years 10
```
