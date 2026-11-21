# InkyPi Development Quick Start

## Development Without Hardware

The `--dev` flag enables complete development without requiring:

- Raspberry Pi hardware
- Physical e-ink displays (Inky pHAT/wHAT or Waveshare)
- Root privileges or GPIO access
- Linux-specific features (systemd)

Works on **macOS**, **Linux**, and **Windows** - no hardware needed!

## Setup
<table>
<tr>
<td>
Traditional setup method

```bash
# 1. Clone and setup
git clone https://github.com/fatihak/InkyPi.git
cd InkyPi

# 2. Create virtual environment
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 3. Install Python dependencies and run
pip install -r install/requirements-dev.txt
bash install/update_vendors.sh
python src/inkypi.py --dev
```

</td>
<td>
Alternative method using devbox - works on macOS / any Linux distro / WSL2

```bash
setopt INTERACTIVE_COMMENTS # enable interactive comments in zsh

# 1. Install devbox and direnv (direnv is optional but recommended)
command -v devbox >/dev/null || curl -fsSL https://get.jetify.com/devbox | bash
command -v direnv >/dev/null || nix profile install "nixpkgs#direnv" \
  && eval "$(direnv hook ${SHELL##*/})" >> ~/.${SHELL##*/}rc \
  && source ~/.${SHELL##*/}rc # If not already present: install direnv, add hooks to shell rc and activate

# 2. Clone and setup
git clone https://github.com/fatihak/InkyPi.git
cd InkyPi # direnv reads .envrc -> runs devbox shell -> installs deps & activates venv

# 3. Run InkyPi in developer mode via devbox
devbox run dev # alternatively run `devbox shell` and then run `python src/inkypi.py --dev`
```

</td>
</tr>
</table>

**That's it!** Open http://localhost:8080 and start developing.

## What You Can Do

- **Develop plugins** - Create new plugins without hardware (no Raspberry Pi, nor physical displays)
- **Test UI changes** - Instant feedback on web interface modifications  
- **Debug issues** - Full error messages in terminal
- **Verify rendering** - Check output in `mock_display_output/latest.png`
- **Cross-platform development** - Works on macOS, Linux, Windows

## Essential Commands

<table>
<tr>
<td>

Traditional activation method

```bash
source venv/bin/activate             # Activate virtual environment
python src/inkypi.py --dev           # Start development server
deactivate                           # Exit virtual environment
```

</td>
<td>
devbox / direnv method

```bash
devbox run dev # run InkyPi in dev mode, terminating deactivates `devbox shell`

# direnv will activate / deactivate `devbox shell` automatically when entering
# and leaving the project directory (provided `direnv allow` has run once)... 
# Otherwise to manually activate / deactivate:
devbox shell                         # Installs deps, and activates Python virtual environment
python src/inkypi.py --dev           # Start development server
exit                                 # Exit devbox shell and deactivates Python virtual environment
```

</td>
</tr>
</table>

## Development Tips

1. **Check rendered output**: Images are saved to `mock_display_output/`
2. **Plugin development**: Copy an existing plugin as template (e.g., `clock/`)
3. **Configuration**: Edit `src/config/device_dev.json` for display settings
4. **Hot reload**: Restart server to see code changes

## Testing Your Changes

1. Configure a plugin through the web UI
2. Click "Display" button
3. Check `mock_display_output/latest.png` for result
4. Iterate quickly without deployment

## Other Requirements

InkyPi relies on system packages for some features, which are normally installed via the `install.sh` script. **(Skip if using devbox method)**

### Linux
**(Skip this section if using devbox method)**

The required packages can be found in this file: 

https://github.com/fatihak/InkyPi/blob/main/install/debian-requirements.txt

Use your favourite package manager (such as `apt`) to install them.

### Chromium or Google Chrome browser

InkyPi uses `--headless` mode to rendering HTML templates to PNG images using a Chrome-like browser.  

Different platforms have different available browser packages, refer to the recommended packages in the table below: 

| Platform | Recommended Package | Notes |
| --- | --- | --- |
| Raspbian / Debian | chromium-headless-shell | chromium or google-chrome will also work if in PATH  |
| All other Linux | chromium | devbox method installs chromium on Linux, as `chromium-headless-shell` is usually not packaged |
| macOS | Google Chrome | chromium on macOS / aarch64 is not considered stable,<br>For devbox method, Google Chrome must be installed at its default location: <br>`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`|
| Windows | Chromium or Google Chrome | devbox method installs chromium on WSL2,<br>if using native Windows (not devbox via WSL2), chromium or google-chrome should also work if in PATH | 

InkyPi will search for a Chrome-like browser in the project's PATH (if using devbox method) and your system's PATH in that order. 

## Refresh requests

`POST /update_now` (plugin form) and `POST /display_plugin_instance` (saved playlist item) return HTTP 202 with an ID and a status URL in the JSON and `Location` header. Poll that URL for `queued`, `running`, `completed`, `superseded`, or `failed`. Completion includes frames that already match the display. HTTP 503 means the worker is stopped.

The worker finishes its current panel operation. One manual request can wait behind it, and a newer request replaces the waiting one. The last 128 terminal results are kept in memory. An expired or pre-restart ID returns HTTP 404. Scheduled work uses the same worker. A due playlist check runs on restart when a prior refresh time is available.

`GET /api/current_image` serves the last published panel frame. Generating a plugin image or a playlist thumbnail does not publish it. A driver failure preserves the previous PNG. Select **Force panel refresh** on the plugin form or append `?force_display=1` to either POST to resend identical content. The first update after a worker process restart reconciles the panel once. Driver acknowledgement records a successful return from the driver. A visual check is still needed on physical hardware.

### Physical drivers and resource limits

Physical writes have a 180-second application deadline. On expiry the request fails, and the last acknowledged frame stays available. The outstanding vendor call retains exclusive access to the panel. While it is running, even a forced retry fails promptly. After it returns, inspect the panel and use **Force panel refresh** to reconcile it. A permanently stuck vendor call requires restarting the service. The deadline applies to writes after driver initialization. It does not change the panel waveform, refresh cadence, or vendor BUSY timing.

The shared image loader accepts up to 16 MiB per transfer and 40 million source pixels. Its decoded-pixel ceiling is 8 million on devices with less than 1 GiB RAM and 32 million elsewhere. JPEG draft decoding can bring a larger source within the decoded budget when resizing. Connection and read timeouts are capped at five seconds per operation. The transfer deadline is checked as chunks arrive. The OS resolver and underlying connection retries can add time before the transfer starts. Low-memory devices spool incoming images to disk. Wikipedia Picture of the Day uses the same limits in original-size and resized modes, limits metadata to 256 KiB, and keeps one panel-sized fallback of at most 2 MiB. A compatible fallback shows its source date during an outage.

On Linux, HTML screenshots run in a dedicated process group and browser profile. A 90-second wall-clock deadline terminates the group and removes its temporary files. For a repeatable workstation comparison, run `python scripts/benchmark_display.py --baseline /path/to/previous/checkout --renderers`. The report includes pixel hashes, median frame preparation time, and sampled RSS for the process and its descendants. RSS sums can count shared pages more than once. Measure the installed service cgroup on the Pi for the 200 MiB service limit.

Plugins are registered at startup and instantiated on first use. A plugin with a missing optional dependency reports that error when selected. On Linux, `python scripts/benchmark_service.py --baseline /path/to/previous/checkout` starts temporary loopback-only instances from committed source. It compares idle usage and the stock clock, then measures the candidate with Chromium rendering a local page. Install the development requirements and Chromium first. The report also gives proportional set size (PSS), which apportions shared pages across processes. The benchmark uses the mock display.

For a disposable-card trial, keep the prior checkout and a copy of its device settings. To roll back this batch, stop the test service, select the refresh-scheduling revision `1a13835` in that checkout, and restart it with the same configuration. This batch does not migrate the device or playlist configuration format. Confirm that the clock refreshes and that the expected playlist is selected.
