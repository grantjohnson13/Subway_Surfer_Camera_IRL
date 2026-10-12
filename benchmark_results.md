# Swipe injection benchmark

Run: 2026-10-12 16:31 — host arm64 macOS 26.5.2, device emulator-5554 (1080x2400), 5 trials x 4 directions per method, swipe length 0.28×width, travel 40 ms in 6 moves, hold 16 ms, centre (0.5, 0.6).
Host↔guest clock offset +272.39 ms (±0.32 ms), corrected for.

Latencies are from the host starting the swipe to the SwipeTest app's UI thread receiving the event. **cross** = the finger has travelled 40 dp, which is roughly when a game decides "that was a swipe" — this is the number that matters for gameplay.

| method | works | success | cross median (ms) | cross p95 | DOWN median | UP median | host call blocks (ms) | startup (ms) |
|---|---|---|---|---|---|---|---|---|
| adb_input | yes | 20/20 | 74.6 | 89.4 | 36.8 | 95.3 | 99.1 | 0 |
| persistent_shell | yes | 20/20 | 58.2 | 61.4 | 14.8 | 77.8 | 0.1 | 3 |
| minitouch | **no** — minitouch could not open the touch device: open: Permission denied | — | — | — | — | — | — | — |
| uiautomator2 | yes | 20/20 | 92.7 | 124.2 | 28.7 | 200.0 | 214.8 | 817 |
| scrcpy | yes | 20/20 | 31.2 | 36.8 | 2.3 | 59.7 | 58.1 | 196 |
| emu_console **(chosen)** | yes | 20/20 | 31.0 | 36.7 | 1.2 | 59.2 | 58.9 | 2 |
| pyautogui_mirror | yes | 17/20 | 59.3 | 69.9 | 27.8 | 124.6 | 123.1 | 3129 |

**Chosen: `emu_console`** — median 31.0 ms to swipe recognition (p95 36.7 ms), 1.2 ms to first touch, 20/20 delivered.

Selection rule: 100% delivery required; methods within 5 ms of the fastest median recognition time are treated as tied (MOVE events are batched to the 60 Hz display frame, so smaller differences are run-to-run noise), and the tie is broken by DOWN latency, which is pure transport time.

## In the real game (bench/game_swipe_check.py)

The probe app proves delivery; the game has its own rules. Subway Surfers (Unity) samples touches once
per rendered frame, so the finger has to be seen moving on at least ~2 frames:

| swipe shape | result in Subway Surfers (60 fps) |
|---|---|
| 8 ms travel, 1 step, 32 ms hold | **missed jumps** (DOWN and the whole move land in one frame) |
| 24 ms travel, 3 steps, 16 ms hold | 12/12 registered |
| **40 ms travel, 5 steps, 16 ms hold (chosen)** | **11/11 registered**, response visible within 150 ms; more margin when the game drops a frame |

Evidence: `evidence/game_A60_run*.png`, `evidence/game_C60_run*.png`, `evidence/game_check_B_8ms_hold32.png`.

**Emulator frame rate matters more than the injector.** macOS throttles the emulator to ~12-15 fps whenever its
window is covered by other windows (measured with `bench/game_fps.py`: 13.5 fps covered vs 60.0 fps visible),
adding 50-100 ms per frame. `scripts/start_emulator.sh` parks the emulator at the top-left and the camera
preview opens beside it; the preview shows the game's live fps and warns if it drops.

## Notes per method

- **adb_input** — New adb connection + a fresh `input` process (app_process JVM start) per swipe.
- **persistent_shell** — Saves the adb connection, but Android still starts an `input` JVM per swipe; returns instantly (fire-and-forget) so no back-pressure.
- **minitouch** — Needs write access to /dev/input/event*; SELinux (enforcing) denies it on Android 10+ without root, and Play Store images cannot `adb root`.
- **uiautomator2** — JSON-RPC over HTTP to an on-device uiautomator server; per-call overhead and coarse 5 ms move steps.
- **scrcpy** — Long-lived scrcpy-server (video off) injecting MotionEvents via InputManager; tiny binary messages over an adb-forwarded socket. Also works on real phones.
- **emu_console** — Emulator telnet console `event mouse`: host-side, feeds the virtual touchscreen below Android; no adb or on-device process in the path. Emulator-only.
- **pyautogui_mirror** — Drags the real macOS cursor over a scrcpy mirror window: extra hops (macOS event -> scrcpy -> device), hijacks your mouse, and drops swipes when the window is not focused.

Failed trials:

- pyautogui_mirror: left — no touch registered within 3 s
- pyautogui_mirror: right — no touch registered within 3 s
- pyautogui_mirror: up — no touch registered within 3 s
