# Submission verification — 2026-10-24

Branch: `release/submission`, based on `origin/feat/look-ui` at `44d47f3`.
This is a setup/offline verification, not a new phone or field-accuracy benchmark.

## Results

- Python 3.12.3; fresh, non-system-site-packages venv.
- `pip install -r requirements.txt`: succeeded; `pip check`: no broken requirements.
- Server import and Boson key loading: passed. Only the authorized
  `~/.config/boson/api_key` was read; the key was never printed or copied into Git.
- Owned server at `127.0.0.1:8794`: `/` returned HTTP **200** with HTML.
  Process stopped afterward; rebinding 8794 confirmed release.
- Full backend suite: **173 tests passed** (at commit 3175db2; 235 after merging the final feat/look-ui, development venv), both in the supplied development venv
  and the fresh core-only venv. This includes four portability regression tests.
- `node --check web/*.js` succeeded; each `web/*.js` was also checked individually
  (Node does not reliably treat multiple arguments as multiple files to check).
- Runtime-code scan of `backend/`, `web/`, `probes/`: no remaining hardcoded
  `/data/`, `/home/`, `/Users/` or Windows drive paths. Historical docs retain
  their original evidence paths.
- Missing-fixture runs of `selftest.py`, `selftest.py --chorus` and opt-in vision
  integration printed explicit **SKIP** messages before API/model/server work.
- The unit suite emits existing `AsyncMockMixin._execute_mock_call` unawaited-
  coroutine warnings in mocked visual-expert tests (`test_expert.py`,
  `server.py:691`). Tests pass; this warning was not fixed or suppressed here.

## Exact fresh-clone commands that worked

Run from the submission worktree after pushing the portability commit:

```bash
cd /data/build/field-companion-sub
git clone --single-branch --branch release/submission "$(git remote get-url origin)" /data/build/tmp/fresh-clone
cd /data/build/tmp/fresh-clone
python3.12 -m venv .venv
mkdir -p out/submission
.venv/bin/python -m pip install -r requirements.txt > out/submission/pip-install.log 2>&1
.venv/bin/python -m pip check
(cd backend && ../.venv/bin/python -m unittest discover -p 'test_*.py') > out/submission/backend-unit.log 2>&1
```

The following startup check was also executed verbatim from that fresh clone.
Do **not** enable shell tracing (`set -x`) around credential assignment.

```bash
set -e
.venv/bin/python -c 'import sys; sys.path.insert(0, "backend"); import server; assert server.app is not None; assert server.api_key(); print("server import + key loading: PASS")'
# Refuse an occupied port; do not stop/reuse someone else's server.
.venv/bin/python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",8794)); s.close()'
BOSON_API_KEY="$(cat "$HOME/.config/boson/api_key")" .venv/bin/python -B backend/server.py --host 127.0.0.1 --port 8794 > out/submission/server-8794.log 2>&1 &
pid=$!
trap 'kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true' EXIT
ready=0
for attempt in $(seq 1 100); do
  if curl -fsS http://127.0.0.1:8794/ -o out/submission/home.html; then ready=1; break; fi
  sleep 0.1
done
test "$ready" = 1
curl -sS -o /dev/null -w 'GET /: HTTP %{http_code}\n' http://127.0.0.1:8794/ | tee out/submission/http-result.log
kill "$pid"
wait "$pid" || test "$?" = 143
trap - EXIT
.venv/bin/python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",8794)); s.close(); print("8794 released: PASS")'
```

A few connection-refused retries occurred during normal Python startup; the
subsequent check returned `GET /: HTTP 200`. This proves HTTP startup and key
**loading**, not successful authentication or a live Higgs speech session.

## Dependency budget / deliberately skipped installs

Fresh core venv: **295 MiB** on disk (`du -sh .venv`). Sizes below come from
PyPI release JSON metadata, selecting Python 3.12 Linux x86-64 wheels or universal
wheels, not estimates of the full dependency closures:

| Package | Wheel size (MiB) |
| --- | ---: |
| birdnetlib 0.18.1 | 58.27 |
| tensorflow 2.19.1 | 615.13 |
| torch 2.8.0 (PyPI, **not CPU-index wheel**) | 846.78 |
| torchvision 0.23.0 (PyPI) | 8.23 |
| pybioclip 2.1.6 | 0.02 |

`requirements-birdnet.txt` was **not installed**: TensorFlow alone is 615 MiB.
This direct-requirements dry run succeeded, but is not a full dependency check:

```bash
.venv/bin/python -m pip install --dry-run --no-deps -r requirements-birdnet.txt > out/submission/birdnet-direct-dry-run.log 2>&1
```

Pip reused locally cached BirdNET/TensorFlow wheels during that dry run; it did
not install them into the fresh venv. `requirements-vision.txt` and torch were
**not installed**. Torch plus transitive dependencies and BioCLIP model/embedding
downloads exceed the small-install budget; model cache size was not measured.
Use the README's separate CPU-vision installation for the full feature set.

## Evidence and scope

Local, ignored artifacts (not bundled in Git):

- `/data/build/field-companion-sub/out/submission/backend-unit.log`
- `/data/build/tmp/fresh-clone/out/submission/pip-install.log`
- `/data/build/tmp/fresh-clone/out/submission/backend-unit.log`
- `/data/build/tmp/fresh-clone/out/submission/server-8794.log`
- `/data/build/tmp/fresh-clone/out/submission/http-result.log`
- `/data/build/tmp/fresh-clone/out/submission/optional-wheel-sizes.json`
- `/data/build/tmp/fresh-clone/out/submission/birdnet-direct-dry-run.log`

No new real BirdNET/BioCLIP inference, microphone/camera browser tests, live
Higgs/GPT requests, HTTPS phone test or field-accuracy measurements were run.
Existing docs describe earlier live tests and their own limitations.
Ports 8780/8795, tmux and other worktrees were left untouched. No merge, PR,
visibility change or LICENSE was made. Team TODO: choose a license and fill the
README roles before final submission.
