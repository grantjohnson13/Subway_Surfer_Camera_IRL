# Subway Surfers, played with your body

You play the real Subway Surfers in the Android emulator. A Python program watches you through
the Mac's webcam and turns your movements into swipes.

| You do | Game gets |
|---|---|
| move into the **left / middle / right third** of the camera view | the runner goes to that lane: one swipe per lane, so left → right third is two swipes |
| **jump** (body rises) | swipe up |
| **duck** / crouch (body drops) | swipe down |

The camera view is split into thirds (`lateral_mode: lanes`), and the third your body is in is your lane. The
preview draws the two lane lines and highlights your lane. The alternative `lateral_mode: lean` makes each
lean/step relative to your calibrated position one lane change.

```
webcam ─► CameraSource (thread; newest frame only, timestamped at capture)
           └─► MediaPipe PoseLandmarker (lite, ~7 ms on M4 CPU)
                └─► torso centre + torso length ─► One-Euro filter ─► GestureDetector
                                                                        │ offsets in torso lengths from the
                                                                        │ calibrated baseline; fires on the
                                                                        ▼ START of a movement
                    run guard (is a run in progress?) ─► AsyncSwiper ─► emulator console ─► Subway Surfers
```

## Setup (once)

```bash
scripts/setup.sh
```

The script is safe to re-run. It installs:

* Homebrew packages: `openjdk@17`, `python@3.13`, `scrcpy`, `android-commandlinetools`
* Android SDK pieces and an AVD called **SubwayCam**: Pixel 6, Android 15, arm64, **Google Play** image, 6 cores, 6 GB RAM. Your other AVDs are left alone.
* A Python venv with pinned versions (`requirements.txt`)
* The MediaPipe pose models
* The emulator gRPC client
* The SwipeTest probe app, used by the benchmark and tests

Steps that need you (one time):

1. **Install the game.** Run `scripts/start_emulator.sh`, then in the emulator:
   1. Sign in to the Play Store and install **Subway Surfers**.
   2. Open it once.
   3. Finish the age, consent and tutorial screens.
2. **Grant camera access.** The first time the controller opens the camera, macOS asks whether your terminal may use it. Allow it. If you denied it earlier, go to System Settings → Privacy & Security → Camera, enable your terminal, and restart it.

## Play

```bash
./run.sh              # boots the emulator if needed, opens Subway Surfers, starts the controller
./run.sh --dry-run    # detections only; sends nothing
./run.sh --help       # all options
```

**Screen layout.** The game is parked full-height on the right of the main display (a TV works
well) and the camera preview fills the rest, so you can see the lane lines while you play
(`preview.layout: split`, `preview.game_side: right|left`; `manual` uses `preview.x/y/width`).
The camera is picked by name (`camera.name: "USB Webcam"`), falling back to `camera.index`.

**Keep the emulator window uncovered.** macOS throttles a covered emulator to 12–15 fps, versus
60 fps when visible. That adds 50–100 ms of lag per frame and makes the game miss swipes. The
preview shows the game's live fps, in red with a warning if it drops.

**How to play:**

1. Stand in the middle of the camera view with shoulders and hips in view. Hold still about 2 s until the calibration bar fills.
2. **Raise both hands above your head** (hold ~0.4 s) to start a run. It taps the PLAY button, or a
   "Tap to continue" overlay in front of it, only when it can see one. After a crash, PLAY takes about
   10 s to appear (the "Continue?" popup times out by itself), and it waits for it. You can also click PLAY with the mouse.
3. Play.

**Recalibrate** with `c` whenever you change position.

**The run guard.** Swipes are only sent while a run is actually in progress, which it checks by
looking for the in-game pause button. Menus and dialogs have buttons exactly where swipes land;
the "Continue?" dialog's *watch an ad* button sits at screen centre. The overlay shows
`game: not in a run – swipes held` when gestures are being held back. If the guard can't see
the screen, it holds swipes; it never sends them blindly.

**Keys:**

* `q` / `Esc`: quit
* `c`: recalibrate
* `space`: pause/resume sending swipes
* `d`: show/hide the debug view (skeleton, trigger lines, stats); hidden by default (`preview.debug`)

**The overlay shows:**

* the skeleton
* the baseline cross-hair
* the four trigger lines (left blue, right orange, jump green, duck red)
* your torso centre (yellow dot), with an arrow showing the velocity look-ahead
* latched state and gesture counts
* camera fps and pose time
* game fps and run state
* **latency from frame capture to swipe sent**

**Other options:**

* `--camera 1`: another camera. An iPhone Continuity Camera can do 60 fps; the MacBook camera tops out at 30.
* `--events out.jsonl`: logs every gesture with its latency
* `--record-landmarks rec.jsonl`: records your movements for tuning and regression tests
* `--video file.mp4`: runs on a recording instead of the webcam
* `--method scrcpy`: uses another injector
* `--no-gate`: disables the run guard (not recommended)

## Recording a session (for videos)

```bash
./run.sh --record     # play as usual; everything goes to recordings/<date_time>/
```

| File | What |
|---|---|
| `camera.mp4` | the clean webcam feed, 1280×720, 30 fps (not mirrored) |
| `overlay.mp4` | the preview as on screen (mirrored, lane lines) |
| `game.mp4` | the emulator screen at full resolution, 1080×2400, 60 fps (scrcpy; the game stays at 60 fps) |
| `markers.json` | start time, each hands-up round start (file time and time of day), and how the game video was aligned |

All three videos start at the same instant and are the same length, so they line up at 0 in any
editor. The game video is aligned by matching every run start in it with the moment the run guard
saw it live (within about 15 ms).

**Syncing another camera, silently.** Every file has a time-of-day timecode from the Mac's clock.
Record the other camera with time-of-day timecode too (on an iPhone, for example the Blackmagic
Camera app's timecode setting), then in Premiere select the clips and choose *Synchronize →
Timecode*. Phone and Mac clocks are both network-synced but can differ by a frame or two. To check
or fine-tune, the preview flashes white when recording starts and at each hands-up round start;
film the TV in the other shot and match the flash.

**Or let the scripts sync it.** For clips without timecode (or to double-check):

```bash
.venv/bin/python edit/sync_angles.py ~/Downloads/Angle_1.mov ~/Downloads/Angle_2.mov
.venv/bin/python edit/make_premiere_xml.py     # -> recordings/SubwaySurfers_edit.xml
```

`sync_angles.py` matches each clip to its session by your body's up/down motion (pose tracking on
both videos, cross-correlated) and writes `recordings/<session>/angles.json`. `make_premiere_xml.py`
builds one vertical 60 fps sequence with every take back to back, each view on its own track
(V1 game, V2 overlay, V3 camera, V4 other angle), in sync and untouched, with a marker at every
take and round. In Premiere: *File → Import* the XML, then double-click the sequence in the Project
panel (dragging it onto a timeline nests it).

## How lanes work

The controller knows which lane the game should be in. Every frame, it sends the swipes needed to make the
game's lane match yours.

* **Run start:** every run starts in the middle lane. When a run starts, the controller waits `lanes.resync_delay_s` for the intro, then moves the runner to the lane you're standing in.
* **Early switching:** a lane switch uses your *projected* position (position + velocity × `lookahead_s`). A fast step switches up to `lanes.lead` before you reach the line.
* **No flicker:** you must be `lanes.hysteresis` past a line to switch, so standing on a line never makes the runner flicker.
* **Limitation:** the controller can't see whether the game accepted a lane change. If a train blocks the move (the runner stumbles and stays put), the controller and the game are one lane out of step until the next run. If the runner seems one lane off, crash or restart the run to re-sync.

The overlay shows "your lane" and "game lane".

## How a gesture fires (jump, duck, and lean mode)

All numbers come from `config.yaml`. Offsets are in torso lengths; velocities are in torso lengths per second.

A gesture fires on the frame where **all** of these hold:

1. `offset ≥ min_offset`: you have actually moved.
2. `velocity ≥ min_velocity` toward that side: deliberate, not drift.
3. `offset + velocity × lookahead_s ≥ trigger`: your *projected* position crosses the line.
   This is why it fires early. In tests it fires 100–133 ms into a 300 ms lean or duck and 100 ms into a jump, roughly the first third of the movement.

After it fires:

* **Latch / hysteresis.** The gesture is locked until you come back below `min(release, half of how far you went)`. It can't release while you're still moving outward, and a short, natural return still re-arms. One duck is one roll, however long you hold it.
* **Cooldown.** The same gesture can't re-fire within `cooldown_s`.
* **Jump → duck block.** After a jump, duck is blocked for `vertical_cross_block_s`, so the landing knee-bend isn't a duck. A jump is *not* blocked after a duck (`duck_blocks_jump_s: 0`): a deep wind-up dip may register as a duck, and in the game the jump simply cancels the roll.
* **Axis dominance.** Vertical gestures need vertical speed of at least `axis_dominance` × horizontal speed (and vice versa). A sideways lean that sags doesn't duck.
* **Drift absorption.** While nothing is latched and you're nearly still, the baseline slowly follows you (`baseline_adapt_tau_s`).
* **Lost tracking.** If tracking drops for more than `lost_reset_s`, filters reset, so re-acquiring you isn't a fake fast move.

## Tuning (hands-on)

1. Record a **guided** session. The preview shows big prompts (go to LEFT lane, JUMP, …), and each prompt is logged with its time:
   ```bash
   .venv/bin/python -m sscam.app --dry-run --guided --record-landmarks logs/tuning.jsonl --events logs/tuning_events.jsonl
   ```
2. Score it. Each prompt is matched to what was detected: correct, missed, or false.
   ```bash
   .venv/bin/python bench/score_guided.py logs/tuning.jsonl logs/tuning_events.jsonl -v
   ```
3. Try changes on the **same** recording, with no re-recording:
   ```bash
   .venv/bin/python bench/score_guided.py logs/tuning.jsonl logs/tuning_events.jsonl --set gestures.duck.trigger=0.4 -v
   ```
4. For any recording, `bench/analyze_recording.py` shows peak offsets and velocities and near-misses.
5. When a change scores better, put it in `config.yaml`.

The current values were tuned this way on two recorded sessions (2026-10-12).

| Symptom | Change |
|---|---|
| Lanes too wide (big steps needed) | narrow the middle lane, e.g. `lanes.boundaries: [0.46, 0.54]` (current; thirds = `[0.333, 0.667]`) |
| Lanes switch when you only sway | widen the middle lane (`[0.42, 0.58]`) or raise `lanes.hysteresis` |
| Lane flickers when standing near a line | raise `lanes.hysteresis` (0.03 → 0.05) |
| Lane switches too late | raise `lanes.lead` or `lookahead_s` |
| (lean mode) lean doesn't register | lower `gestures.left/right.trigger` (0.32 → 0.26) or `min_velocity` (0.8 → 0.6) |
| A jump misses (small hop) | lower `jump.trigger` (0.18 → 0.14) or `jump.min_velocity` |
| Ducks when you land from a jump | raise `vertical_cross_block_s` (0.55 → 0.75) |
| Rolls when you dip before jumping | raise `duck.trigger` / `duck.min_offset` (each +0.06 costs about one frame of duck latency) |
| Ducks are missed | lower `duck.trigger` (0.34 → 0.28); keep your hips in frame when crouching |
| Can't do two quick lane changes | lower `left/right.release` or `cooldown_s` |
| Fires too late | raise `lookahead_s` (0.12 → 0.16); lower `min_offset` |
| False triggers while standing still | lower `filter.min_cutoff_hz` (2.5 → 1.5), or use `pose.model: models/pose_landmarker_full.task` |
| Game ignores some swipes | check the game fps in the overlay (uncover the emulator); then raise `injector.swipe.duration_ms` |

Tips:

* Stand about 2–3 m from the camera, with your whole body in frame even when you jump.
* Use even lighting and avoid strong backlight.

## Latency (measured)

| stage | time | how measured |
|---|---|---|
| movement start → gesture decided | 100–133 ms | synthetic sequences and real-person video; the body must move ~0.1–0.2 torso lengths before it's distinguishable from jitter |
| camera frame interval | 33 ms (30 fps) | MacBook camera max in every mode |
| **frame capture → swipe sent** | **7–8 ms median** (pose ~7 ms) | live pipeline driving a real game run (`logs/live_video_into_game.log`); 8.3 ms live on the webcam |
| swipe sent → Android has the touch | 1.2 ms | `benchmark_results.md` |
| swipe sent → swipe recognisable (40 dp travelled) | 31 ms median, 37 ms p95 | `benchmark_results.md` |
| game reacts | 1–2 frames at 60 fps; visible within 150 ms | `evidence/game_A60_run*.png` |

From frame capture to the game having a recognisable swipe takes about **40 ms**. On top of that come
the camera's own sensor-to-driver delay (not measurable here; typically 30–60 ms for built-in webcams)
and the time your body takes to move far enough to be told apart from jitter.

## Input injection

`bench/benchmark_injection.py` benchmarks all methods against the probe app. Results and the
reasoning for the choice are in [`benchmark_results.md`](benchmark_results.md).

* **Fastest:** the emulator console (`event mouse`, host-side; chosen) and scrcpy control (automatic fallback). Both are ~1–2 ms to first touch and ~31 ms to recognition.
* **Slower:** `adb shell input` 75 ms, persistent shell 58 ms, uiautomator2 93 ms.
* **Blocked:** minitouch, by SELinux on Android 15.
* **Unreliable:** the pyautogui mirror, 17/20.

**Swipe shape** (`injector.swipe`) was tuned in the real game with `bench/game_swipe_check.py`.
Unity samples touches once per frame, so the finger must be seen moving on at least ~2 frames:

* An 8 ms flick missed jumps.
* 40 ms of travel plus a 16 ms hold registered every swipe.

## Tests

```bash
.venv/bin/python -m pytest -q        # 93 tests, ~45 s
```

* `tests/test_detector.py`: synthetic landmark sequences covering:
  * lanes mode: stepping between thirds, two-lane moves, standing on a line (no flicker), drift, calibrating in a side lane
  * regressions from the real sessions: deep wind-up dip then jump, early fire not self-releasing, short returns re-arming, per-axis baseline correction
  * each gesture firing exactly once and early
  * standing still with jitter and slow drift (never fire)
  * held leans, wobble and partial returns
  * jump preparation dips and landing squats
  * lean with sag, jumping while leaning
  * pose dropouts
  * near/far distance, at 30 and 60 fps
* `tests/test_pipeline_video.py`: a generated video of a real person goes through MediaPipe, the detector and a dry-run injector end to end. Also covers "standing still fires nothing" and record/replay.
* `tests/test_injection.py`: swipe geometry, config validation and run-guard fail-closed logic. With the emulator running, it also sends all four swipes with each configured method and verifies them in the probe app.

## Tools

| | |
|---|---|
| `bench/benchmark_injection.py` | injector latency benchmark → `benchmark_results.md` |
| `bench/game_swipe_check.py` | plays short runs and sends swipes; per-run contact sheets of the game's reaction |
| `bench/game_fps.py` | the game's real frame rate (SurfaceFlinger) |
| `bench/drive_game_with_video.py` | full live pipeline from a gesture video into a real game run |
| `bench/analyze_recording.py` | tuning report from a `--record-landmarks` session |
| `bench/score_guided.py` | scores a `--guided` session against a config (try changes on the same recording) |
| `edit/sync_angles.py` | syncs other-camera clips to recorded sessions by body motion |
| `edit/make_premiere_xml.py` | Premiere project (FCP7 XML) with all takes and views synced |

## Troubleshooting

* **Game feels laggy, or swipes get missed.** Check the game fps in the overlay. If it's below about 45, the emulator window is covered; move other windows off it.
* **"game-state guard unavailable".** The emulator wasn't started with gRPC. Use `scripts/start_emulator.sh`, or `./run.sh`.
* **"cannot open camera".** Grant camera permission (see Setup), and quit other apps using the camera.
* **Gestures show "held" in the overlay.** No run is in progress. Start one in the game.
