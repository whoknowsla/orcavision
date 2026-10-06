# OrcaVision for Orca

An Orca user extension that describes what is on screen with a vision model
and presents the description through Orca's speech and braille. It works on
X11 and Wayland.

Providers: OpenAI, Claude (Anthropic), Google Gemini, a local Ollama server,
or any OpenAI-compatible server (OpenRouter, Groq, Mistral, LM Studio,
llama.cpp, vLLM and others; set its base URL, such as
`https://openrouter.ai/api/v1` or `http://localhost:1234/v1`).

## Commands

| Keys | Command |
| --- | --- |
| Orca+Ctrl+D | Describe the active window |
| Orca+Ctrl+Shift+D | Describe the whole desktop |
| Orca+Ctrl+Alt+D | Describe the focused object |
| Orca+Ctrl+Alt+K | Store or remove an API key in the keyring |
| (none) | Describe the image on the clipboard |
| (none) | Repeat the last description |
| (none) | Cancel the description in progress |

Commands without keys can be bound in Orca's preferences, under OrcaVision.
Starting a new description replaces one that is still in progress. Each
description is also copied to the clipboard (this can be turned off).

## Install

```sh
./install.sh            # copies orcavision/ into ~/.local/share/orca/extensions and approves it
orca --replace &        # restart Orca to load it
```

Run `./install.sh` again after any change: Orca refuses to load an extension
whose contents changed until it is approved again.

## Settings

Open Orca's Preferences, go to User Extensions, select OrcaVision and press its
settings button. Settings are stored in Orca's settings (dconf), under
`/org/gnome/orca/default/extensions/orcavision/settings`. API keys are not.

## API keys

Press Orca+Ctrl+Alt+K, choose the provider, type the key into the masked
field and press Save. The key goes straight into your keyring; it is never
written to Orca's settings, and Orca does not echo it while you type.

The dialog shows which providers already have a stored key, in a summary line
and next to each provider's name, for example "OpenAI (key stored)". It
checks without reading the keys or unlocking the keyring. "Remove Stored Key"
appears only when the selected provider has a stored key.

Where keys are kept is set by "Where to store API keys" in the settings.
Automatic uses the first of these that is ready: a Secret Service keyring that
is running and has a working default keyring, a KDE Wallet that is running, or
the encrypted file. A keyring that is only installed, or whose default keyring
is missing or broken, is skipped, because using it would mean a setup or
unlock prompt that may never appear. Choose it explicitly to use it anyway.

| Storage | Used by | Notes |
| --- | --- | --- |
| Secret Service keyring | GNOME Keyring, KeePassXC (Secret Service integration), KDE Wallet 6, others | Stored with libsecret. |
| KDE Wallet | KDE Plasma 5 and 6 | Through kwalletd's own D-Bus interface, for setups without Secret Service. |
| Encrypted file | systemd 256 or later | `systemd-creds --user` encrypts each key with the host's credential key (and the TPM when present) into `~/.local/share/orca/orcavision-keys/`; only you, on this machine, can decrypt it. |

Your keyring may ask you to unlock it the first time a key is stored or read.
If it does not answer within a minute, OrcaVision gives up and says so.

When no key is stored, OrcaVision uses the provider's environment variable,
if Orca's environment has it: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`GEMINI_API_KEY` (or `GOOGLE_API_KEY`), `OPENAI_COMPATIBLE_API_KEY`.

Earlier versions kept keys in Orca's settings in plain text. OrcaVision moves
any such key into the keyring when it starts and removes it from Orca's
settings.

Claude models think before answering and the thinking counts toward the
output limit, so Claude always gets at least 16000 output tokens; the prompt
keeps descriptions short. On current Claude models OrcaVision also enables
server-side refusal fallbacks, which re-run a declined request on another
Claude model.

## X11 and Wayland

On X11 the screen is captured directly; libwnck 3 finds the active window
when installed, otherwise Orca's view of the active window is used.

Wayland does not let apps read the screen, so OrcaVision uses the first of
these that works:

| Desktop | Tool | Active window |
| --- | --- | --- |
| Sway, Hyprland | `grim` (with `swaymsg` / `hyprctl`) | yes |
| Other wlroots compositors | `grim` | no |
| KDE Plasma | `spectacle` | yes |
| GNOME | `gnome-screenshot` | yes |
| Any desktop | xdg-desktop-portal screenshot | no |

Without a tool that can capture the active window, OrcaVision captures the
whole screen and asks the model to describe only that window. The focused
object is handled the same way, because Wayland apps do not know where their
windows are. Some desktops ask for permission the first time the portal is
used. Clipboard images are read with `wl-paste` (from wl-clipboard).

## Requirements

- Orca 51 or later.
- Only the Python standard library and the GObject libraries Orca already
  uses, plus the Wayland tools above where needed.

## Tests

```sh
python3 -m pytest tests/
```

The tests load the package the way Orca's extension loader does, and one test
checks it against the Orca installed on the machine.
