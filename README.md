# OmniGuide

**A screen-aware AI desktop companion for visual, step-by-step software guidance.**

OmniGuide runs as a compact desktop launcher on Linux/KDE. Describe what you want to do, and it captures the current desktop, asks Muse Spark Open to interpret the screen, and highlights the relevant interface element with a click-through overlay and animated pointer.

## Current status

This repository includes the desktop prototype **v1.8.4**. It supports screen questions, locating visible UI elements, and generating short visual walkthroughs. During a walkthrough, the user advances each step with the on-screen action button or the configured keyboard shortcut (`Ctrl+Shift+Space` by default).

The overlay is intentionally click-through: OmniGuide does not intercept or automate clicks. Step progression is user-triggered; automatic background click detection is not implemented in this version.

## Features

- Compact launcher with a system-tray resident mode.
- Screen-aware Q&A and visual UI-element location.
- Short walkthroughs with highlighted targets, an animated pointer, and instruction bubbles.
- Separate LayerShellQt overlay process for KDE Wayland, keeping the launcher a normal window.
- Spectacle-based desktop capture and compressed screenshot upload to the configured vision model.
- Optional launch-at-login entry managed from the tray menu.

## Requirements

- Linux with KDE Plasma; the current overlay implementation targets **Wayland**.
- Python 3.10 or newer (developed/tested with Python 3.14).
- Spectacle (`spectacle`) for desktop capture.
- LayerShellQt and its Qt 6 QML module for the Wayland overlay.
- A Baseten API key with access to `meta-models/muse-spark-open-1026`.

On Fedora, install the desktop components (package names can vary by Fedora release):

```bash
sudo dnf install spectacle layer-shell-qt
```

## Setup

1. Clone the repository and enter its directory.
2. Create and activate a virtual environment:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

3. Install Python dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Create the local environment file and add your Baseten key:

   ```bash
   cp .env.example .env
   ```

   Edit `.env` and set `BASETEN_API_KEY` to your own key. **Never commit `.env`.**

5. Start OmniGuide:

   ```bash
   python omniguide.py
   ```

`omniguide.py` is the primary desktop launcher. The older `omniguide_desktop*.py` and `omniguide_palette_v*.py` files are development prototypes retained for project history. `app.py` is a separate Streamlit prototype, not the main desktop launcher.

## Using OmniGuide

- Launch `python omniguide.py` to open the command palette.
- Ask a question about the visible screen, request an element to be highlighted, or ask for a walkthrough (for example, “Show me how to save this file”).
- Follow the highlighted target and instruction.
- Use the **I completed this step** button or your KDE global shortcut (`Ctrl+Shift+Space` by default) to let OmniGuide capture the updated screen and evaluate the next step.
- Use the tray icon to reopen OmniGuide, cancel guidance, manage launch-at-login, or quit.

To configure the global shortcut, add a KDE Custom Shortcut that runs:

```bash
python /absolute/path/to/OmniGuide/omniguide.py
```

The command summons the existing resident instance when it is running. You can also start the app resident in the background with:

```bash
python omniguide.py --background
```

## Privacy and limitations

- OmniGuide captures the desktop only when a request or guide step requires it. The captured screenshot is compressed and sent to the configured Baseten inference endpoint for analysis.
- Do not use it on screens containing sensitive information unless you are comfortable sending that screenshot to the configured inference provider.
- The overlay passes mouse input through to the application underneath. It does not click controls on the user's behalf.
- This prototype does not continuously monitor the desktop or automatically detect every mouse click. Users advance walkthrough steps explicitly.
- Accurate targeting depends on the screenshot, model response, display scaling, and visible UI state. If a target is unclear, retry the step.

## Project technology

- Python / PySide6 for the desktop application.
- Qt Quick / QML and LayerShellQt for the Wayland overlay.
- Spectacle and Pillow for screenshot capture and image preparation.
- OpenAI-compatible Python client pointed at Baseten inference.
- Muse Spark Open (`meta-models/muse-spark-open-1026`) for visual understanding.

## Configuration

The desktop app reads `.env` from the same directory as `omniguide.py` and expects:

```env
BASETEN_API_KEY=your_baseten_api_key
```

The inference base URL and model are currently configured in the application source.
