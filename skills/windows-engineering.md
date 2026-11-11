# Windows Systems Engineering Standards

## 1. Operational Baseline

- Identify the Windows edition, build, architecture, PowerShell version, current principal, and management boundary before choosing commands.
- Prefer reversible, idempotent automation. Capture the original state, verification method, and rollback before changing the registry, services, drivers, firewall, or security policy.
- Use the least privilege required. Do not run installation, recovery, or security-changing commands automatically with elevation.
- Treat production, domain, and personal workstations as different scopes. Test destructive or disruptive operations in an isolated environment first.

## 2. PowerShell and Automation

- Set explicit error behavior in scripts and use `try`/`catch`/`finally` around bounded operations. Preserve the original exception and avoid silent fallback.
- Support pipeline input and structured output where useful. Avoid parsing formatted text when CIM, .NET, or cmdlet objects expose the data directly.
- Prefer CIM cmdlets over legacy WMI cmdlets in new PowerShell Core automation. Filter at the provider with `-Filter` when it reduces data transfer.
- Use runspaces or `ForEach-Object -Parallel` only when their overhead and thread-safety model fit the workload. Protect shared state and bound concurrency.
- Prefer `System.Collections.Generic.List<T>` over repeated array concatenation for large mutable collections. Do not force garbage collection without measured evidence.
- Sign operational scripts where policy requires it. Execution Policy is not a complete security boundary; use WDAC, Constrained Language Mode, or JEA for stronger control.

## 3. Packages, Updates, and Deployment

- Prefer `winget` with explicit package identifiers and inspected source agreements. Use Chocolatey only where it is already managed and trusted.
- Review `winget upgrade --all` before applying it. Pin or exclude packages whose upgrades require separate validation.
- Use DISM for image and component-store servicing and `Add-WindowsDriver` for deliberate offline driver injection. Understand that `/ResetBase` limits later uninstall options.
- Windows Deployment Services and MDT support PXE, task sequences, driver selection, and repeatable deployment. Validate boot images and network impact before production rollout.
- Delivery Optimization can distribute update content across peers. Verify topology, privacy, and bandwidth policy before enabling it.

## 4. Services, Tasks, and Process Management

- Inspect service binary path, account, dependencies, recovery actions, and trigger start before changing startup behavior.
- Use `Get-Service`, `Set-Service`, and `sc.exe` with their actual syntax. Keep service recovery commands separate from application configuration.
- Understand that `svchost` hosts grouped services and that splitting varies by build and configuration. Diagnose the contained service rather than treating every `svchost` instance as suspicious.
- Task Scheduler and WMI event subscriptions can be legitimate automation or persistence. Inventory task principals, triggers, actions, event filters, consumers, and bindings.
- For process diagnostics, inspect handles, working set, commit, thread state, and loaded modules. Sysinternals `handle.exe` and Process Explorer can supplement built-in tools.

## 5. Networking and Name Resolution

- Use `Get-NetIPAddress`, `Get-NetTCPConnection`, `Test-NetConnection`, `pktmon`, and bounded `netsh trace` captures according to the required layer.
- Disable LLMNR through Computer Configuration > Administrative Templates > Network > DNS Client > **Turn off multicast name resolution** after confirming DNS fallback behavior.
- NetBIOS over TCP/IP is separate. Inventory adapters with `Win32_NetworkAdapterConfiguration` and use its `SetTcpipNetbios` method with `TcpipNetbiosOptions = 2` only for authorized adapters.
- Do not substitute `ms_tcpip6`: that component controls IPv6, not LLMNR or NetBIOS over TCP/IP.
- RSS, RDMA, SMB Direct, offload features, interrupt moderation, jumbo frames, and Packet Direct require compatible adapters, switches, and workloads. Measure before tuning.
- IPsec connection-security rules, BranchCache, DirectAccess, and Always On VPN address different network boundaries. Document authentication, routing, and rollback for each.

## 6. WSL2, Hyper-V, and Isolation

- Configure WSL resource limits in `.wslconfig` and distribution-specific behavior in `/etc/wsl.conf`. Verify whether mirrored networking is supported before enabling it.
- WSL2 uses a lightweight utility VM. Custom kernels, systemd, file-path translation, and Windows/Linux interop must be tested together.
- Hyper-V supports nested virtualization, dynamic memory, and GPU partitioning on compatible systems. Validate host build, hardware, driver, and guest support.
- Windows Sandbox provides disposable isolation for suspicious files. Application Guard provides a separate browser or Office isolation boundary where supported.

## 7. Windows Internals

- The Object Manager namespaces devices, drivers, and base objects; handles reference objects through access-checked process tables.
- Process and thread management includes scheduling, priority boosts, wait states, and context switching. Do not infer root cause from CPU usage alone.
- Virtual Address Descriptors describe process memory ranges. Distinguish private commit, shared mappings, working set, non-pageable pool, and page-file usage.
- I/O Request Packets traverse the driver stack. Filter drivers and minifilters can affect storage behavior, latency, and access control.
- PnP builds the device-node tree and coordinates with the Power Manager across system and device power states. Use `powercfg /requests` for application or driver sleep blockers.
- The Cache Manager provides lazy writing, read-ahead, and shared file caching. Analyze memory pressure together with storage latency rather than treating cache use as a leak.

## 8. Storage, ReFS, and Recovery

- Storage Spaces and Storage Spaces Direct provide different local and clustered pooling models. Document redundancy, tiering, rebuild, and backup behavior.
- ReFS offers integrity streams, block cloning, sparse VDL, and accelerated VHDX operations on supported configurations. Confirm feature support before selecting it.
- Use `diskpart`, `stordiag.exe`, SMART data, filesystem logs, and latency evidence for storage diagnosis. Set read-only attributes deliberately during forensic acquisition.
- WinRE and WinPE support offline recovery, driver addition, and hive inspection. Preserve BCD and registry backups before `bcdboot`, `bootrec`, or offline registry work.
- Registry hives, transaction logs, redirection, and `WOW6432Node` affect offline and 32-bit views. Do not assume a single registry path represents every process view.

## 9. Security and Identity

- AppLocker and WDAC provide application control; ASR rules, CFG, VBS, Credential Guard, and LSA Protection address different attack surfaces.
- Use Virtualization-Based Security to protect supported credential paths. Validate hardware, driver, and application compatibility before enforcement.
- Windows Hello for Business and smart cards use key-backed authentication and TPM or PIV hardware. Keep recovery and revocation procedures explicit.
- Kerberos FAST, EPA, restricted NTLM, gMSA, and tiered administration reduce identity risk. Avoid granting broad `SeDebugPrivilege` or backup privileges for routine tasks.
- AD CS certificate templates and AD FS federation require separate threat models. Review enrollment, issuance, signing, SAML/OIDC claims, and rollback.
- Windows firewall rules should identify program, direction, protocol, scope, and owner. Avoid default-deny changes without a verified management path.

## 10. Group Policy and Enterprise Configuration

- Evaluate local, site, domain, and OU policy order together with enforcement, block inheritance, security filtering, WMI filters, and loopback mode.
- Group Policy Preferences can configure user-adjustable state. Never deploy legacy `cpassword` material; inspect existing preference XML for exposed credentials.
- MSI troubleshooting uses logs, transforms, and servicing metadata. Avoid manual deletion of Installer registry branches or obsolete utilities such as MsiZap.
- DNS scavenging, DNSSEC, DHCP failover, clustering quorum, and Cluster-Aware Updating require infrastructure-wide planning and tested recovery.

## 11. Diagnostics and Performance

- Use WPR for bounded traces and WPA for CPU, disk, DPC/ISR, and stack analysis. Store traces in the designated evidence directory and stop capture deterministically.
- Performance Monitor data collector sets can track custom thresholds. Processor queue, disk latency, available memory, paging, and `% Idle Time` are signals, not universal standalone thresholds.
- Use WinDbg `!analyze -v`, module lists, thread stacks, `!locks`, and wait-chain evidence for bugchecks and deadlocks. Confirm symbols before naming a faulty module.
- ETW providers support real-time and recorded diagnostics. `logman`, `pktmon`, and `netsh trace` expose different capture scopes and overhead.
- Resource Monitor exposes per-process disk, network, memory, and wait-chain views. Correlate them with event logs and application telemetry.

## 12. Forensics and Event Logs

- Prefetch, ShimCache, Amcache, LNK files, Jump Lists, Shellbags, Recycle Bin metadata, and the USN Journal provide different evidence with different limitations.
- `$I` Recycle Bin metadata can contain original path and deletion time; `$R` contains the corresponding content. Preserve both during acquisition.
- Event IDs 4624, 4625, 4688, and 1102 are useful only with logon type, auditing policy, principal, host, and surrounding events. A single ID is not a complete incident conclusion.
- Preserve original artifacts, timestamps, hashes, and acquisition notes. Do not analyze the only copy of an evidence hive or log.

## 13. Remote Management and Web Infrastructure

- WinRM over HTTPS requires correct listeners, certificates, SPNs, and client trust. Do not use wildcard `TrustedHosts` as a convenience default.
- JEA combines role capabilities, session configurations, and constrained endpoints to expose only authorized commands.
- RDP Shortpath, NLA, Restricted Admin mode, and graphics settings affect transport and credential exposure. Validate gateway and client support.
- HTTP.sys provides kernel-mode request handling for IIS and other applications. Inspect queues, certificates, application pools, and worker-process isolation before tuning.

## 14. Maintenance and Reporting

- Maintain tested 3-2-1 backups, patch and rollback procedures, centralized logs, least-privilege reviews, and current network and recovery documentation.
- Review Event Logs, component-store health, drivers, services, startup tasks, exposed firewall rules, and certificate expiry on a risk-based schedule.
- Report the exact build and configuration, evidence, minimal remediation, required elevation, verification, rollback, and residual uncertainty. Do not present generic hardening thresholds as universal acceptance criteria.
