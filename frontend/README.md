# Pipeline Bus task visualizer

A zero-dependency, read-only frontend for the `pipeline-bus` repository. It maps
the protocol states to a transport control room rather than a generic Kanban
board.

## Preview locally

From the repository root:

```bash
python3 -m http.server 8000 -d frontend
```

Open `http://localhost:8000`.

The public branch automatically loads `frontend/data/visual-qa.json`, which is
fully synthetic and intentionally contains a long merged-task history to stress
scrolling, alignment, and responsive behavior.

## Capture a browser screenshot

With Chrome, Chromium, or Edge installed:

```bash
python3 frontend/capture-preview.py
```

The default screenshot is written to `frontend/preview.png` and is ignored by
git. Custom viewport example:

```bash
python3 frontend/capture-preview.py \
  --width 1280 --height 1000 \
  --data ./data/visual-qa.json \
  --output /tmp/pipeline-bus-1280.png
```

Set `CHROME_BIN` when the browser executable is not discoverable automatically.

## Automated visual QA

`.github/workflows/visual-preview.yml` runs on changes to the frontend. It:

1. validates JavaScript and Python syntax;
2. captures 1600px desktop, 1280px laptop, and 390px mobile screenshots;
3. uploads them as a `pipeline-bus-visual-preview` workflow artifact.

The screenshots use synthetic data only.

## Data interfaces

The renderer accepts:

- `{ tasks: [...], summary: {...} }` analytics payloads;
- extractor bundles containing `analytics`, `statuses`, `task_specs`, `activity`, and `crew`;
- bare task/status arrays.

Available entry points:

```text
./data/dashboard.json                 optional local live bundle (gitignored)
./data/analytics.json                 optional analytics payload (gitignored)
./data/visual-qa.json                 public synthetic fallback
?data=/path/to/custom.json            URL-selected source
Load JSON                              local file picker
window.pipelineBus.render(payload)    browser/runtime bridge
window.pipelineBus.load(url)          fetch and render a JSON endpoint
pipeline-bus:data                     CustomEvent for live injection
```

## Privacy and scope

- Read-only visual layer; it does not write tasks, status files, or branches.
- No real private task payloads, token sessions, or generated live dashboard data
  belong in this public branch.
- `frontend/data/dashboard.json` and `frontend/data/analytics.json` are ignored.
- The dispatch bay uses the original hand-drawn 70-frame Pipeline Bus GIF stored
  at `frontend/assets/pipeline-bus-clawd-boarding.gif`.
- `prefers-reduced-motion` users receive the static parked frame from
  `frontend/assets/pipeline-bus-parked.png`.
