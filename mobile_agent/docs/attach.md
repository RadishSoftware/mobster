# Experimental app scores

Reviewed 5 November 2026. `mobster doctor --apps` rates the first accessibility screen of each installed app. The flag is hidden from help. Set `MOBSTER_EXPERIMENTAL_ATTACH=1` to enable it. It launches apps and reads their accessibility trees; it does not tap, type or scroll.

The scorer detects WebViews and Flutter-like trees. It does not read a WebView's DOM, execute page JavaScript or rewrite Flutter nodes. The agent loop does not consume this experimental package.

## Modules

| Module | Responsibility |
|---|---|
| `attach/cli.py` | Lists installed apps, launches each one, reads its first screen, then stores and prints the scores. |
| `attach/score.py` | Rates a screen and records its reasons; keeps the latest score per bundle in `<data dir>/attach/app-scores.json`. |
| `attach/web.py` | Finds the largest shown WebView and tests which accessibility elements lie inside it. |
| `attach/flutter.py` | Recognises Flutter engine marks or a tree with mostly labelled `Other` nodes and few typed controls. |

## Rating rules

A screen is poor when it has three or fewer elements, or fewer than 60% of its controls are labelled. It is partial when fewer than 85% are labelled, it has fewer than eight elements, it exposes no controls, or it contains a WebView or Flutter-like tree. Otherwise it is good. A poorer condition wins when several apply.

The thresholds and Flutter shape heuristic use synthetic fixtures. They are not calibrated against a real-app survey. The score describes one screen, not the whole app.

## Removed adapters

The experimental Web Inspector bridge was removed on 3 November 2026. The unused DOM parser, JavaScript actions and Flutter node adapters were removed on 5 November 2026. They had no production callers. The accessibility scorer and its detections remain.
