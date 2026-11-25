# Network Quality for Alfred 6

An Alfred UI window for macOS's built-in `networkQuality` speed test. It shows Watch-style dials that update live, a latency readout, "good for" tags (4K streaming, video calls, gaming, large uploads) and a history you can filter by network.

<!-- Screenshot: add one here -->

## Install

Download `Network.Quality.alfredworkflow` from [Releases](../../releases) and double-click it. You need Alfred 6 with the Powerpack, and a recent macOS. It has only been tested on the current release.

## Use

- **`nq`** opens the window. ↩ runs or stops a test, ⌘C copies the result, ⌘/ opens the help sheet, and the dropdown filters history by network.
- **`nqh`** lists recent results in Alfred. Type a network name to filter. ↩ copies a result, ⌘↩ opens the window.
- If you close the window mid-test, the test still finishes, saves the result and posts a notification.

## Configure

In Alfred → Workflows → Network Quality → Configure Workflow:

- **Keywords** for the window and the history.
- **Auto-start**: run a test as soon as the window opens.
- **Test mode**: parallel (default), sequential, download only or upload only.
- **Thresholds** for the Poor / Okay / Great grades:

  | Measure | Default | Source |
  |---|---|---|
  | Download | 25 / 100 Mbps | FCC broadband benchmarks, 2015 and 2024 |
  | Upload | 3 / 20 Mbps | FCC broadband benchmarks, 2015 and 2024 |
  | Responsiveness | 300 / 1000 RPM | IETF responsiveness draft (poor / fair) |
  | Latency | 30 / 100 ms | lower is better |

## How it works

`scripts/nq.py` runs `networkQuality` in a pty so it can read the live progress lines. It eases the values and writes SVG dial frames, which the window picks up through Alfred UI's `pulse` at about 10 fps. Each result is saved with the network name, the router's MAC address and whether a VPN was on, so you can compare a network with itself over time.
