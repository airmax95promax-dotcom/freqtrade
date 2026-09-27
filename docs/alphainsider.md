# AlphaInsider paper-trade mirror

Freqtrade can optionally mirror confirmed dry-run fills to an AlphaInsider paper strategy.
This integration is an RPC handler, not an exchange adapter: Freqtrade continues to simulate
orders through `dry_run`, and AlphaInsider receives a matching paper market order only after
Freqtrade emits an `entry_fill` or `exit_fill` event.

## Safety properties

- The handler refuses to start unless `dry_run` is `true`.
- Only confirmed fill events are mirrored.
- A persistent event hash prevents repeat delivery after restart.
- An ambiguous network result engages a persistent safety lock. It is never retried blindly.
- API credentials are read from configuration/environment and are never written to state.
- Pairs without an explicit AlphaInsider mapping are rejected.

This is still a mirror: Freqtrade and AlphaInsider maintain separate paper ledgers. Monitor and
reconcile both before relying on performance results.

## Configuration

Keep the token outside the JSON configuration:

```bash
export FREQTRADE__ALPHAINSIDER__API_KEY='your-token'
export FREQTRADE__ALPHAINSIDER__STRATEGY_ID='your-strategy-id'
```

Add the non-secret configuration:

```json
{
  "dry_run": true,
  "alphainsider": {
    "enabled": true,
    "pair_map": {
      "BTC/USDT": "BTC-USD:COINBASE"
    },
    "state_file": "user_data/alphainsider_mirror_state.json",
    "timeout": 10
  }
}
```

The token must include the AlphaInsider `newOrder`, `getOrders`, and `getPositions` scopes. On
startup, the handler verifies the token and refuses to run if verification fails. The base URL
is restricted to HTTPS on `alphainsider.com` so a configuration mistake cannot send the token
to another host.

## Safety-lock recovery

If a submission times out or returns an invalid acknowledgement, the state file is marked
`locked`. Compare the event recorded under `uncertain` with the AlphaInsider order history.
Resolve the remote order first. Only then make a backup, clear the matching uncertain record,
set `locked` to `false`, and restart Freqtrade.

Never delete the state file to bypass an unresolved order: doing so can duplicate a paper order.
