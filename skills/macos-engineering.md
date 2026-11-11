# macOS Systems Engineering Standards

## 1. Operational Baseline

- Identify the macOS version, hardware architecture, current user, and available privileges before choosing commands. Do not assume Apple Silicon, Intel, or a particular shell.
- Prefer reversible, idempotent changes. Record the original state, verification command, and rollback for system-affecting work.
- Do not execute `sudo`, disable security mechanisms, delete system data, or change recovery settings without explicit user authorization.
- Separate observed evidence from recommendations. Test third-party tools and Homebrew formulae against the actual host rather than assuming they are installed.

## 2. Homebrew and Software Management

- Use `brew install --formula <name>` for command-line packages and `brew install --cask <name>` for supported desktop applications.
- Inspect before changing: `brew info <name>`, `brew outdated`, and `brew doctor`. Review what `brew upgrade` will change instead of upgrading blindly.
- Use a `Brewfile` with `brew bundle` for reproducible workstation configuration. Keep proprietary taps and pinned versions explicit.
- Use `brew services` only after inspecting the generated service definition and its user context. Run `brew cleanup` after reviewing size and version implications.
- `brew doctor` is a diagnostic aid, not an integrity proof. Verify package provenance and signatures separately when required.

## 3. Launchd and Background Services

- Distinguish Launch Daemons (`/Library/LaunchDaemons`, system context) from Launch Agents (`/Library/LaunchAgents` and the user domain).
- Inspect plist ownership, permissions, `Label`, `ProgramArguments`, `RunAtLoad`, `KeepAlive`, `StartInterval`, stdout, and stderr before loading a service.
- Use modern `launchctl bootstrap`, `bootout`, `enable`, `disable`, and `print` forms with the correct domain, such as `gui/$(id -u)`. Avoid deprecated `load` and `unload` workflows.
- Make service startup idempotent. Prevent restart loops with explicit error handling, bounded logs, and deliberate throttle settings.

## 4. TCC, SIP, Gatekeeper, and Platform Security

- TCC controls access to protected user data and devices. Request the minimum permission and tell the user when Full Disk Access, camera, microphone, or accessibility access is required.
- Do not edit TCC databases directly. Use System Settings, documented profile management, or `tccutil reset` only for a deliberately selected application and scope.
- Check System Integrity Protection with `csrutil status`. SIP protects system locations even from root; changing it requires recovery-mode action and explicit authorization.
- Gatekeeper evaluates quarantined software. Inspect quarantine attributes before removing them; never recommend blanket quarantine stripping for untrusted downloads.
- XProtect provides Apple-managed malware detection. Notarization adds an Apple malware scan for distributed software but does not replace local code review.
- Secure Enclave protects cryptographic keys and biometric operations. Treat it as a hardware security boundary, not a general-purpose secrets database.
- ASLR, non-executable memory, signed code, sandbox profiles, and privilege separation provide complementary exploit mitigation.

## 5. Signing, Entitlements, and Notarization

- Inspect signatures with `codesign -dv --verbose=4 <path>` and verify them with `codesign --verify --deep --strict <path>` where applicable.
- Keep entitlements minimal and declarative. Review hardened-runtime exceptions individually; an entitlement is not proof that an action is safe.
- Use `notarytool` for notarization and `stapler` to attach the ticket to the distributed artifact. Verify the final artifact, not only the build output.
- Prefer DriverKit, Endpoint Security, Network Extensions, and other supported user-space frameworks over legacy third-party kernel extensions.

## 6. XNU, Mach, BSD, and I/O Kit

- XNU combines Mach task, thread, virtual-memory, and IPC mechanisms with the BSD process, POSIX, networking, and filesystem layers.
- Mach ports are IPC endpoints with receive, send, and send-once rights. Port sets support multiplexed waiting. Validate message ownership, rights, lifetime, and buffer handling in IPC designs.
- XPC is the preferred high-level mechanism for privilege-separated application services. Avoid presenting raw Mach messaging as the default application IPC API.
- Mach VM uses memory objects, pagers, page protection, inheritance, and copy-on-write. Distinguish wired, compressed, clean, dirty, and swap-backed memory when diagnosing pressure.
- Mach exceptions can be delivered to exception ports and translated into POSIX signals. Use Crash Reporter and DiagnosticReports evidence before inferring a kernel fault.
- The scheduler maps application QoS and workload behavior onto performance and efficiency cores. Do not assume deterministic real-time latency from QoS labels alone.
- I/O Kit provides driver objects, matching, registry publication, and power-state management. Use `ioreg` for registry inspection and account for deprecated legacy driver APIs.

## 7. Apple Silicon and Rosetta 2

- Apple Silicon uses unified memory shared by CPU, GPU, and Neural Engine. Zero-copy opportunities do not eliminate synchronization or lifetime requirements.
- Performance and efficiency cores require measurement under representative workloads. Avoid fixed core-count and frequency assumptions.
- Rosetta 2 combines dynamic translation with cached ahead-of-time translation where available. Build universal binaries when native performance or framework compatibility matters.

## 8. APFS, Storage, and FileVault

- APFS containers share space among volumes. Snapshots are point-in-time container state; clones share extents through copy-on-write.
- Use `diskutil list` and `diskutil apfs list` for topology. Inspect local snapshots with `tmutil listlocalsnapshots /` before deleting any snapshot through an authorized maintenance operation.
- Use `diskutil verifyVolume` for non-destructive checks. Reserve `fsck` and container repair for an appropriate recovery environment with a verified backup.
- FileVault protects data at rest. Preserve recovery-key and institutional escrow procedures before changing encryption or startup security.
- HFS+ remains relevant for legacy media; do not assume APFS features such as snapshots and clones exist on every mounted volume.

## 9. Logging, Diagnostics, and Performance

- Use `log show` for persisted evidence and `log stream` for live investigation. Filter by process, subsystem, category, and time to bound output.
- Unified Logging combines memory and disk-backed stores with activity tracing. Log retention and privacy redaction affect what can be proven later.
- Use `sysdiagnose` for a broad diagnostic snapshot and `spindump` or `sample` for hung-process stack evidence. Treat these as potentially sensitive artifacts.
- DTrace can instrument kernel and user-space events, but SIP and platform restrictions may prevent probes. Check permissions before designing a DTrace-based check.
- Use `powermetrics` for SoC power, frequency, and thermal evidence with appropriate authorization. Use `vm_stat`, `memory_pressure`, `iostat`, and `top` for initial triage.
- Diagnose kernel panics from `/Library/Logs/DiagnosticReports`, recent driver or system-extension changes, and reproducible triggers rather than panic text alone.

## 10. Networking and Packet Filter

- Inspect configuration with `networksetup` and the dynamic store with `scutil`. Use `scutil --dns` for resolver state and `netstat -rn` for routes.
- Network Extensions provide supported filtering, VPN, DNS, and content-control mechanisms. Avoid deprecated NKE designs.
- PF configuration lives in `/etc/pf.conf`. Validate rules before loading them with `pfctl`; changing the firewall requires explicit authorization and a rollback path.
- Use `tcpdump` for bounded packet capture and `dnctl` for controlled latency, loss, or bandwidth tests. Captures may contain private data.
- Tune TCP buffers, MTU, jumbo frames, and link aggregation only from measured link and endpoint capabilities. Do not apply generic 10GbE sysctl values to unknown networks.

## 11. MDM, Automation, and Forensics

- Configuration profiles declaratively manage Wi-Fi, certificates, restrictions, and enrollment. Inspect profile payloads and signing before installation; profile removal may require administrative rights.
- AppleScript, JXA, Shortcuts, Swift command-line tools, and `osascript` serve different automation boundaries. Choose the smallest supported interface and avoid brittle UI scripting.
- Back up plist files before using `defaults` or `/usr/libexec/PlistBuddy`. Validate the resulting plist and application behavior.
- Useful artifacts include Unified Logs, quarantine events, Safari history, application containers, LaunchServices records, and DiagnosticReports. Preserve timestamps, ownership, and chain of custody.
- Quarantine and browser databases are SQLite artifacts, not proof of execution by themselves. Correlate them with logs, signatures, and filesystem metadata.

## 12. Hardware and Peripheral Subsystems

- `pmset` exposes sleep, display, power-assertion, and hibernation settings. Inspect current values with `pmset -g` and sleep blockers with `pmset -g assertions` before changes.
- SMC, NVRAM, battery, and thermal controls vary by hardware generation. Do not apply Intel-era SMC reset instructions to Apple Silicon.
- Core Audio uses the HAL and Audio Units for low-latency audio. Diagnose routing, sample rate, buffer size, and third-party virtual devices separately.
- Metal benefits from unified memory on Apple Silicon, while external display control may involve I2C or DDC/CI utilities. Validate third-party display tools before installation.
- Inspect USB and HID hierarchies with `ioreg`. Wi-Fi diagnostics may involve CoreWLAN, system logs, and optional tools such as `blueutil`; do not assume the retired `airport` utility is available.

## 13. Maintenance, Recovery, and Spotlight

- Verify Time Machine or another backup by restoring a sample, not only by checking that a backup exists.
- Review `softwareupdate -l`, LaunchDaemons, LaunchAgents, obsolete configuration profiles, and third-party system extensions during maintenance.
- Use Apple Diagnostics, Safe Mode, Recovery, Share Disk, and startup-security controls according to the exact hardware and macOS version.
- For Spotlight problems, check `mdutil -s /` and enabled content types first.
- Rebuild a specific volume or folder through System Settings > Spotlight > Search Privacy: add the location to exclusions, wait a few seconds, then remove it from exclusions.
- When a full CLI rebuild is explicitly authorized, use `sudo mdutil -E /` and allow indexing to finish. Never delete Spotlight index directories manually.

## 14. Engineering Report

Report the observed macOS and hardware versions, evidence gathered, minimal change, privileged operations, verification, rollback, and remaining uncertainty. Avoid generic checklists when a focused command or reproduction provides stronger evidence.
