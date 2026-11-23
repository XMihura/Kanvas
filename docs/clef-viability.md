# Clef in Kanvas: viability report

Date: 2026-11-20. Branch `feat/clef`. Status: **measured against Cloudflare's Clef on 2026-11-20 and 2026-11-23.** Of the four
uses first tried, two were kept (missing dependencies and duplicate proposals), and a third was added later: the
order of work that would collide. Note kinds and summary scores are removed from the code.

## Decision

| Use | Decision | Threshold | Right when shown | Recall | Calibration error | Latency avg / p95 | Input tokens per call | USD, life of a 15-task board |
|-----|----------|----------:|-----------------:|-------:|------------------:|------------------:|----------------------:|-----------------------------:|
| Missing dependencies | **Keep** | 0.60 | 95 % (20 of 21) | 69 % | 0.05 | 1.26 / 4.74 s | 2306 | 0.012 |
| Duplicate proposals | **Keep** | 0.70 | 100 % (15 of 15) | 83 % | 0.05 | 1.02 / 2.42 s | 3135 | 0.009 |
| Note kinds | Removed | — | 100 % (18 of 18) at 0.70 | — | 0.23 | 0.43 / 0.81 s | 370 | — |
| Summary scores | Removed | — | 70 % (16 of 23) at 0.70 | — | 0.18 | 0.42 / 0.69 s | 485 | — |

"Right when shown" is the share of what the board would show that matches the labels; recall is the share of the true
dependencies or repeats it shows.

- **Missing dependencies: kept, at 0.60.** The rule set beforehand: use 0.60 if precision stays at 90 % or more. It is 95 %, and
  recall rises from 52 % to 69 % (table below).
- **Duplicate proposals: kept, at 0.70.** Every flag was right, and the three repeats it missed are all cases marked
  "hard".
- **Note kinds: removed.** Every kind Clef was sure of was right. But 28 % of the notes stayed under the threshold, and
  calibration was poor (0.23): Clef was right even when it said 0.45. The agent reads the note itself anyway.
- **Summary scores: removed.** Accuracy was 43 %, too low to show.

## The real run

`python3 tests/clef_eval.py --real -v --json clef-results.json` on 2026-11-20, all four uses, threshold 0.70:

| Use | Cases | Calls | Answered | Accuracy | Under threshold | Right when shown | Recall | Calibration error | Latency avg / p95 | Input tokens per call |
|-----|------:|------:|---------:|---------:|----------------:|-----------------:|-------:|------------------:|------------------:|----------------------:|
| Note kinds | 25 | 25 | 100 % | 100 % | 28 % | 100 % of 18 | — | 0.23 | 0.43 / 0.81 s | 370 |
| Missing dependencies | 22 | 22 | 100 % | 96 % | — | 100 % of 15 | 52 % | 0.05 | 1.26 / 4.74 s | 2306 |
| Duplicate proposals | 24 | 24 | 100 % | 99 % | — | 100 % of 15 | 83 % | 0.05 | 1.02 / 2.42 s | 3135 |
| Summary scores | 18 | 18 | 100 % | 43 % | 57 % | 70 % of 23 | — | 0.18 | 0.42 / 0.69 s | 485 |

- **No call failed.** Clef answered all 89 calls, and Kanvas read every answer.
- **Clef accepted the request format:** choice options as `{option: description}`, score levels as a list, and a
  yes/no question without `criteria`. Clef refused no call.
- **Cost of the run:** 144k input tokens, about 0.035 USD. The runner had estimated 83k tokens (0.02 USD).

The results files (`clef-results.json`, `clef-eval.log`) stay outside the repository.

### Thresholds

Scored again from the saved answers with `--from` (nothing sent):

| Threshold | Dependencies: right when shown | Recall | Duplicates: right when shown | Recall |
|----------:|-------------------------------:|-------:|-----------------------------:|-------:|
| 0.50 | 95 % of 22 | 72 % | 94 % of 17 | 89 % |
| 0.55 | 95 % of 21 | 69 % | 100 % of 16 | 89 % |
| **0.60** | **95 % of 21** | **69 %** | 100 % of 15 | 83 % |
| 0.65 | 95 % of 20 | 66 % | 100 % of 15 | 83 % |
| **0.70** | 100 % of 15 | 52 % | **100 % of 15** | **83 %** |
| 0.80 | 100 % of 11 | 38 % | 100 % of 13 | 72 % |

- **The one wrong dependency at 0.60** is case d18: Clef says SM-04 at 0.66, and the case is marked "hard".
- **Indirect dependencies.** At 0.60 the board would also show 10 suggestions for tasks the task needs only through
  others (6 at 0.70). They are not wrong, just one step removed. The measures leave them out, and the board stops
  showing one when the direct dependency is added.
- **Missed dependencies at 0.60:** 9 of 29. Clef gave most of them less than 0.32; two are "hard" cases.
- **Duplicates stay at 0.70**, as decided beforehand. 0.55 would add one right flag, but 0.50 brings the first wrong one.

### Calibration

Both kept uses have a calibration error of 0.05. Above 0.7, every answer was yes. Under 0.1, every answer was no:
169 of 169 for dependencies, 366 of 366 for duplicates. In the middle the numbers mean what they say. For example,
the 6 dependency answers between 0.6 and 0.7 averaged 0.66, and 5 of them (83 %) were yes.

### Latency

Cloudflare documents about 200 ms. Measured, the averages were about 1 s, and p95 was 4.7 s for dependencies and 2.4 s
for duplicates, above the 1 s pass mark set before the run. The kept uses hold nothing: the server asks in the
background, and a suggestion appears a few seconds after the change. Only note kinds held anything (the agent's notice,
up to 3 s), and they are removed.

### Tokens

Real input tokens were 1.77 times the old estimate of four characters per token. The ratio per call ranged from 1.71 to
1.94. The estimate (`kanvas.clef_tokens`) now counts one token per 2.25 characters, which is within 10 % of every
measured call (0.96 to 1.09). The costs below use it.

## Cost per board

`python3 tests/clef_eval.py --costs` counts the input tokens of the real questions over a board's life, at 0.24 USD per
million input tokens (output is free). It assumes this life for every task: an agent proposes it (one duplicate check),
then it is created, approved and edited once (three dependency checks). A task the human creates gets no duplicate
check, so these figures are a ceiling.

| Board | Tasks | Calls | Dependencies | Duplicates | All tokens | USD |
|-------|------:|------:|-------------:|-----------:|-----------:|----:|
| Weather dashboard (`examples/sample.json`) | 15 | 60 | 51k | 38k | 90k | 0.022 |
| Drone (`examples/drone.json`) | 13 | 52 | 40k | 29k | 69k | 0.017 |
| Website relaunch (`tests/fixtures/phases.json`) | 25 | 85 | 67k | 89k | 155k | 0.037 |
| Tetris (`tests/fixtures/tetris.json`) | 21 | 81 | 129k | 77k | 206k | 0.049 |
| 105 tasks (Tetris five times) | 105 | 840 | 5784k | 1979k | 7763k | 1.863 |

Both uses ask one question per other task, so their cost grows with the square of the board's size. A board of a few
dozen tasks costs cents over its whole life; one of a hundred costs about two dollars. The largest call, 64 candidates
on the 105-task board, is about 12k tokens. That is well under Clef's 65,536-token context.

## What Clef sees when it is on

With `KANVAS_CLEF=1` and both credentials in the environment of `kanvas serve`, the server sends Cloudflare:
- the board's title;
- task IDs, titles and states;
- descriptions, clipped to 600 characters for the task asked about, 240 for the other tasks listed.

**Missing dependencies** lists the task and the unfinished tasks it could still wait for. **Duplicate proposals** lists
the proposal and every other task, done ones too. Notes, finish summaries, images and the project's files are never
sent.

The API token stays in memory. It is not written to the board, the page or the state folder, and redirects are
refused. Keep Clef off for boards whose content must not leave the computer.

## How to run the evaluation

The cases are the example boards in this repository; no private data is sent.

```bash
export KANVAS_CLEF=1 CLOUDFLARE_ACCOUNT_ID=... CLOUDFLARE_API_TOKEN=...
python3 tests/clef_eval.py --real -v --json clef-results.json       # 46 calls, about 126k tokens, about 0.03 USD
python3 tests/clef_eval.py --from clef-results.json --threshold 0.65 # score the saved answers again; sends nothing
python3 tests/clef_eval.py --costs                                   # cost over a board's life; sends nothing
```

Each use is scored at its own threshold (`kanvas.CLEF_THRESHOLDS`), or at `--threshold` for all. `-v` lists every
wrong answer, and the JSON file keeps every answer.

**On macOS**, python.org's Python ships without CA certificates, so every call fails with `CERTIFICATE_VERIFY_FAILED`.
Set `SSL_CERT_FILE=/etc/ssl/cert.pem`, or run **Install Certificates.command** in its folder in Applications. Kanvas's
log message for this failure says so.

To try it on a real board, start `kanvas serve` from this branch with the three variables set. `serve` says "Clef: on" when it is.

## Order of work that would collide

When a task becomes ready or starts, Kanvas asks whether it would change the same code as each approved task in
progress or ready to start (`clash:ID`). 16 labelled cases (`tests/clef_cases.json`, `order`), on the recipe book and a
notes app board, several taken from
collisions seen when agents worked side by side on the recipe book.

| Threshold | Shown | Right when shown | Found |
|-----------|-------|------------------|-------|
| 0.40 | 8 | 7 (88 %) | 7 of 8 |
| **0.60** | 6 | 6 (100 %) | 6 of 8 |
| 0.70 | 5 | 5 (100 %) | 5 of 8 |

Kept at 0.60: no false alarm. Accuracy on every answer 96 %, 0.35 s per call, about 0.19 USD per 1000 calls. Of the
two misses, one is the other direction of a pair Clef caught (the board shows a pair on both tasks), and the other a
task to "verify and document" offline use against one that builds it; on a running board Clef flagged that second pair at
68 % when the build task started.

## Still open

- **The labels are one person's judgement.** Cases marked `"hard"` in `tests/clef_cases.json` could fairly be labelled
  either way. The one wrong dependency suggestion and all three missed repeats are such cases.
- **The cases are small example boards.** 46 cases is enough to pick a threshold, not to promise a precision on boards
  with longer, vaguer descriptions.
- **Large boards.** The cost grows with the square of the board's size. If that matters, ask only about tasks that share
  words with the task, or only about unfinished tasks for duplicates.
