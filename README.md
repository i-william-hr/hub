# hub — the central service dashboard

> Built with Python, Flask, PyYAML.

One page that answers "what runs on this box and is it up right now". The service list is not copied into it: the app reads this file (data/projects.yaml) at request time, so the panel and the wiki cannot disagree, and a project onboarded through the new-project workflow appears on the panel with no second edit. Each card carries a live up/down dot from a threaded TCP connect to the entry's loopback port; the page refetches every 30s.

## Installation

```bash
git clone https://github.com/i-william-hr/hub.git
cd hub
```

## Usage

```bash
python3 app.py
```

The app.py entry point binds loopback only and expects a reverse proxy in front for TLS.

## License

MIT License — see [LICENSE](LICENSE).
