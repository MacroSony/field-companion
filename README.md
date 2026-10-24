# field-companion (working name)

Hands-free voice field companion for birders and naturalists. Built for the Boson AI × MScAC Higgs Audio Hackathon (Oct 23–24, 2026).

You talk; it logs. BirdNET listens for birds in the background, BioCLIP identifies photos of any organism, and Higgs Realtime handles the conversation and tool calls. The result is a structured checklist that you can export to eBird or iNaturalist.

- Design: [docs/DESIGN.md](docs/DESIGN.md)
- Feasibility tests: [docs/FEASIBILITY.md](docs/FEASIBILITY.md)
- Higgs Realtime probe notes (zh): [docs/realtime-probe-report.zh.md](docs/realtime-probe-report.zh.md)
- Probe scripts: `probes/`. The API key is read from `$BOSON_API_KEY` or `~/.config/boson/api_key`. **Never commit keys.**
- Local setup and Windows commands: [docs/LOCAL_DEVELOPMENT.md](docs/LOCAL_DEVELOPMENT.md).
- GPT Naturalist expert path: [docs/EXPERT.md](docs/EXPERT.md).
