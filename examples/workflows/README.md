# Saved-task recipes

Five saved tasks to start from. Each one only reads, or stops before anything that sends, buys or changes something, so a schedule can't do harm while you try it. Every recipe is saved paused (`"enabled": false`); turn it on once you have run it by hand and like the result.

| recipe | app | what it does | schedule |
| --- | --- | --- | --- |
| `morning-summary.json` | any (Smart) | Reads your notifications and today's calendar, and says what needs your attention | 7:00 every day |
| `reorder-to-cart.json` | Amazon (Smart) | Adds your last coffee order to the cart and stops at the cart: it never checks out | 9:00 on Sundays |
| `unread-replies.json` | Messages | Lists who is waiting on a reply and what they asked; sends nothing | noon on weekdays |
| `price-check.json` | Safari | Reports the lowest price on the first page of results for one product | 8:00 every day |
| `battery-storage.json` | Settings | Reports battery health, storage used and free, and the three largest apps | 18:00 on Sundays |

Change the `goal`, the `cron` and the `timezone` to yours, then add one with the API that `mobster serve` and the Mac app share ([Scripts and the API](https://docs.mobster.dev/scripting#the-http-api)). Needs a running `mobster serve` (its first line names the token file) or Mobster for Mac, and no phone:

```sh
mobster serve > /tmp/mobster-serve.log &
TOKEN=$(cat "$(head -1 /tmp/mobster-serve.log | jq -r .tokenFile)")
curl -s -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  --data @examples/workflows/battery-storage.json http://127.0.0.1:8765/api/workflows
```

Output, from `mobster serve` 0.4.0 on a port it picked, 17 Oct 2026, trimmed with `...` (the `id` is new each time; the answer is one JSON line, shown on several here):

```text
{"workflow": {"id": "a47819193d4e", "name": "Battery and storage report", "appId": "settings", "appName": "Settings",
  "goal": "Open Settings and report the battery's maximum capacity ...", "outputSchema": null, "outputFormat": "auto",
  "engine": "smart", "device": null, "cron": "0 18 * * sun", "timezone": "America/Los_Angeles", "enabled": false,
  "revision": 1, ... "lastRunId": null, "lastOutcome": null, ...}}
```

All five recipes were accepted this way, each saved with `"enabled": false`.

Run a saved task by hand first (`POST /api/workflows/{id}/run` with an `Idempotency-Key` header, or Run in the Mac app), read what it did, and only then turn its schedule on. Approvals still apply to scheduled runs: anything that would send, buy, post or delete waits for you, and with `MOBSTER_ALERT_WEBHOOK_URL` set you hear about it when it waits a minute ([`mobster alerts`](https://docs.mobster.dev/reference/cli#mobster-alerts)).
