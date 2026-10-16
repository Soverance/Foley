# Always-on `foley browse` on Windows

This sets up one Windows machine to run `foley browse` in the background from logon, keep the catalog current with a daily scan, and (optionally) serve the browser to other machines on your network as `http://foley.example.lan/`.

Everything here runs as your normal user. Only the firewall step needs an elevated PowerShell.

## 1. Install and index

Install Foley as in the [README](../README.md#quick-start), in a repo checkout with its own `.venv`:

```powershell
git clone <repo-url> C:\tools\foley
cd C:\tools\foley
uv sync --extra models          # or plain `uv sync` for keyword-only search
```

Put the config in the default data folder, so the task, your shell and the MCP server all find it without flags:

```powershell
New-Item -ItemType Directory -Force "$env:LOCALAPPDATA\Foley" | Out-Null
Copy-Item foley.toml.example "$env:LOCALAPPDATA\Foley\foley.toml"
notepad "$env:LOCALAPPDATA\Foley\foley.toml"   # set [library] root
uv run foley index                             # first full index
```

A library on a NAS share is fine (`root = "//nas.example.local/Audio"`). The task below runs under your logon, so it reaches the share with your own credentials: no stored password.

## 2. Configure the server

In `foley.toml`:

```toml
[browse]
host = "0.0.0.0"                     # every interface; keep "127.0.0.1" for this machine only
port = 8765                          # or 80 for a URL without a port
allowed_hosts = ["foley.example.lan"]

[schedule]
enabled = true
time = "03:00"                       # this machine's local time
catch_up = true                      # scan after logon if the 03:00 slot was missed
startup_delay_minutes = 5            # give the network and the share time to come up
```

`allowed_hosts` lists the names other machines will type. Requests for any other host name get 403. Port 80 needs no admin rights on Windows, but it must not be in use by another web server (IIS, for example).

Read the licence and security notes in [browse.md](browse.md#serving-it-on-your-network) before serving a library to other people: there is no authentication.

## 3. Start it at logon

```powershell
.\scripts\windows\install-browse-task.ps1
```

This registers a Task Scheduler task, **Foley Browser**, for the current user that runs `foley browse --no-open` with the repo's own `.venv\Scripts\pythonw.exe` (no console window) at logon, and starts it now. The task:

- restarts the server up to 5 times, a minute apart, if it exits;
- has no time limit and runs on battery;
- starts in `%LOCALAPPDATA%`, not the repo, so a stray `foley.toml` in the checkout cannot take over;
- logs to `%LOCALAPPDATA%\Foley\logs\browse.log` (size-rotated).

Options:

```powershell
.\scripts\windows\install-browse-task.ps1 -Config D:\foley\foley.toml   # explicit config
.\scripts\windows\install-browse-task.ps1 -LogFile D:\logs\foley.log
.\scripts\windows\install-browse-task.ps1 -NoStart                       # register only
.\scripts\windows\install-browse-task.ps1 -Uninstall                     # stop and remove
```

Run it again after changing options; it replaces the task. After editing `foley.toml`, restart the task (`Stop-ScheduledTask` / `Start-ScheduledTask -TaskName 'Foley Browser'`, or log off and on).

## 4. Open the firewall (elevated)

From an elevated PowerShell in the repo:

```powershell
.\scripts\windows\allow-browse-firewall.ps1 -Port 8765
.\scripts\windows\allow-browse-firewall.ps1 -Port 8765 -RemoteAddress 192.168.10.0/24
.\scripts\windows\allow-browse-firewall.ps1 -Remove
```

It adds an inbound TCP rule, **Foley Browser**, on the Domain and Private profiles only (never Public), limited to the local subnet unless you pass `-RemoteAddress`. The rule is port-based rather than program-based, because a venv's `pythonw.exe` is a launcher and the listening socket belongs to the base interpreter it starts.

## 5. Name it

Create a DNS record, `foley.example.lan`, pointing at the machine (on your router or internal DNS server), and make sure the name is in `allowed_hosts`. Other machines then open `http://foley.example.lan:8765/` (or `http://foley.example.lan/` on port 80).

## Keeping it always on

- **Sleep:** a sleeping machine serves nothing and misses scans. Set the power plan to never sleep when plugged in, or at least to stay awake overnight. Catch-up covers a missed slot, but only after the next start.
- **Logon:** the task starts at *your* logon. If the machine reboots for updates, the server is down until someone logs on. Enable automatic sign-in on a dedicated machine, or accept the gap.
- **Updates:** after `git pull`, run `uv sync --extra models` (it fails while the server holds files in `.venv`, so stop the task first) and start the task again.

## Checking on it

- `http://127.0.0.1:<port>/` on the machine itself.
- The log, `%LOCALAPPDATA%\Foley\logs\browse.log`: the listening address, scan starts and outcomes, and errors (a bad config, a port already in use).
- The Scans dialog in the browser: schedule, next run, history and each scan's log, also kept in `%LOCALAPPDATA%\Foley\logs\`.
- `Get-ScheduledTask -TaskName 'Foley Browser' | Get-ScheduledTaskInfo` for the task's last result.
- `uv run foley status` for catalog health.

## Other platforms

Nothing here is Windows-only except the two scripts. On Linux or macOS, run `foley browse --no-open --log-file <path>` from a systemd user service or a launchd agent, and open the port in your firewall the same way.
