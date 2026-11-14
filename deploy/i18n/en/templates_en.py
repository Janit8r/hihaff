"""English built-in task templates.

nkey is the database primary/unique key. It is an INTERNAL IDENTIFIER and MUST
stay byte-identical to upstream (yes, two of them contain Chinese): changing it
would make the upstream ON CONFLICT (nkey) DO NOTHING seed re-insert the Chinese
originals on the next startup. Only name / description / goal are translated.
The goal text is what actually reaches the LLM.
"""

TASK_TEMPLATES = [
    {
        "nkey": "mobile_app",
        "name": "Mobile App Testing",
        "description": "Managed Android device + dynamic testing of a target app",
        "goal": """Start with device_list to confirm the device is online and check its root status; use device_apps to locate the target app and device_app_info for its install path; launch it with device_url_open; capture key screens with device_screenshot / device_ui. If rooted, attach the recording proxy with device_proxy and capture traffic with device_capture_*. If not rooted, limit yourself to screenshots, files and app management, and state clearly in the conclusion that traffic capture was unavailable.

[WAF CONTROL] Monitor WAF alerts throughout capture and proxy probing; at the first sign of blocking or an IP ban, slow down or stop, and switch to encoded/obfuscated payloads if needed. Never push through with high-rate requests.
[NO BUSINESS IMPACT] Read-only validation only: do not delete or modify app data or business configuration; do not trigger order placement, payment, deletion or authorisation flows that change business state; do not install/uninstall business apps or change their configuration on the device.""",
    },
    {
        "nkey": "miniprogram",
        "name": "Mini-Program Assessment",
        "description": "Burp traffic capture + wxapkg decompilation to find APIs and broken access control",
        "goal": """The decompiled mini-program artefacts are already in the workspace. Steps: 1) read the decompiled folder (app.json / app-service.js) and list backend domains, API paths, appid and hard-coded keys; 2) launch the mini-program on a real device with mini_open or device_url_open and operate it for a few rounds so traffic flows into Burp; 3) use mcp__Burp__get_proxy_http_history_regex to pull requests from Burp by domain/path (do not change Burp's upstream) and get_scanner_issues for existing findings; 4) replay the interfaces found in the decompiled code with mcp__Burp__send_http1_request, modify parameters and strip authentication to test unauthorised access / broken access control / parameter tampering; 5) validate SSRF/XXE with mcp__Burp__generate_collaborator_payload + get_collaborator_interactions; 6) attach request/response evidence to every conclusion and call report_finding.

[WAF CONTROL] Monitor WAF alerts throughout; on a warning (block page / 429 / ban notice) immediately reduce request rate, simplify the payload and pause before continuing; use encoding variants (URL / double encoding, base64, split-and-concatenate) to avoid IP bans; when Burp shows a block page, stop and slow down instead of retrying the same request.
[NO BUSINESS IMPACT] Read-only validation only: do not delete, modify or overwrite business data, business configuration or production code; do not trigger order placement / payment / deletion or anything that changes business state; record the path of any temporary file you upload and remove it during wrap-up.""",
    },
    {
        "nkey": "web 渗透",
        "name": "Web Penetration Test",
        "description": "Standard Web / API assessment",
        "goal": """Read the asset inventory to extract the targets, perform web fingerprinting and sensitive-path discovery, and in parallel reverse engineer site JavaScript to recover backend APIs, request parameters, encryption algorithms and signature-verification logic. Perform non-destructive validation ONLY for the following high-risk classes: SQL injection, unauthorised access to core sensitive information, arbitrary file read, executable file upload, SSRF, and exploitable XSS.

[MANDATORY TEST CONSTRAINTS]
1. Non-destructive: never create, modify, delete or overwrite business data; never write dirty data; no DoS and no destructive payloads; no probe may affect the business.
2. WAF adaptation: watch WAF alerts and blocking behaviour throughout; when triggered, automatically lower request rate and simplify payloads to avoid a source-IP ban.
3. Sensitive credentials: keys, signing keys and tokens recovered from JS are NOT standalone findings; include them in a finding only when they combine with a vulnerability into a complete, reproducible exploit chain.
4. Evidence: every reported vulnerability must carry the original raw HTTP request AND response as reproduction evidence.
5. Alert filtering: discard findings with no real exploitation value; do not report low or ineffective medium severity. Filter at least: version/fingerprint disclosure, missing HTTP security headers, static leaks such as robots.txt, blind injection with no data-exfiltration channel, self-contained non-interactive XSS, and purely informational notices.

[OUTPUT FORMAT]
Follow the ARTEX report template strictly. Keep only high / critical severities - no low or ineffective medium items. Each finding must contain: vulnerability name, affected asset, risk description, exploitation preconditions, reproduction steps, raw request, raw response, remediation. No unrelated chat or process logs.""",
    },
    {
        "nkey": "exe_test",
        "name": "Windows Client Testing",
        "description": "Capture Windows client traffic, inspect configuration and update channels",
        "goal": """Place the target exe in the workspace and statically inspect its import table, strings and signature; at runtime capture its outbound traffic through the recording proxy to find update endpoints and hard-coded credentials; validate privilege-escalation surfaces and persistence locations.""",
    },
    {
        "nkey": "asset_sync",
        "name": "Asset Mapping Sync",
        "description": "Import ScopeSentry scan results into ARTEX",
        "goal": """After a ScopeSentry mapping round has finished, use asset synchronisation to pull project/task assets into ARTEX; verify company / asset ownership, then build the coverage baseline from those assets.""",
    },
    {
        "nkey": "攻防演练",
        "name": "Adversarial Exercise (Red Team)",
        "description": "Full attack-chain exercise: entry to objective",
        "goal": """Extract targets from the asset list, perform fingerprinting and path discovery, and reverse engineer site JS for interfaces, parameters, encryption and signature logic. Conduct adversarial validation against RCE and command-injection vulnerabilities; obtaining a shell is permitted.

MANDATORY CONSTRAINTS:
1. OPERATIONAL BOUNDARY: do not delete, tamper with or overwrite business data; no DoS. Echo / information-gathering payloads and short-lived temporary shells are allowed. NEVER implant a backdoor, write autostart entries, add accounts, establish persistence, move laterally or spread inside the internal network. A shell is used ONLY to prove a vulnerability is real, and "verification only" does NOT mean "delete it once verified": the dropped webshell and its shell_add registration MUST BE RETAINED (record the drop path). A human decides when to remove it in the WebShell page, and whether to remove the remote file too. Only temporary probes/scripts uploaded for analysis are removed at wrap-up.
2. WAF CONTROL: monitor WAF alerts throughout; on a warning, lower request rate, simplify payloads and use encoding variants to avoid an IP ban.
3. SENSITIVE CREDENTIALS: keys, tokens and signing keys found in JS are not standalone findings; include them only when they combine with RCE / command injection into a complete reproducible chain (including shell access).
4. EVIDENCE: every RCE / command-injection conclusion must carry its matching raw HTTP request AND response; retain the key packets from shell acquisition.
5. OUTPUT: report only effective RCE / command-injection findings where commands execute successfully and shell access is stable; filter out false positives, no-echo cases, cases that cannot exfiltrate results and cases with no shell. Do not output other vulnerability classes.

Follow the ARTEX report template, severity critical/high, with fields: vulnerability name, affected asset, risk description, exploitation preconditions, reproduction steps, raw request, raw response, remediation. No unrelated logs or chat.

[AFTER OBTAINING A SHELL]
Register it with mcp__Webshell__shell_add (type: php/php_raw/jsp/aspx, password = the shell's parameter name), then use shell_exex exclusively for operations (shell_exec):
1) Environment: id / whoami / hostname / uname -a / pwd
2) Boundaries: ls -la in the current directory, look for .env, .git, configuration files, database connection strings
3) Evidence: attach the command and raw output to every conclusion and record it with report_finding
4) Files: use shell_download to pull files into the workspace for analysis; use shell_upload only when necessary and record the path
Wrap-up: DELETE ONLY the temporary probes/scripts uploaded for analysis during this run (if one cannot be removed, record its path and why). KEEP the dropped webshell and its shell_add registration - it must remain visible in the WebShell page for human review and re-testing; write the drop path into the report. No backdoors, autostart, accounts, persistence, lateral movement or business-data modification.

[THREE ROUTES TO A SHELL - CHOOSE BY FEASIBILITY]
1) File upload / RCE: payload_list for variants matching the target type (php/jsp/aspx) and WAF strength -> payload_get for the full text -> drop it through the upload point (record the drop path) -> shell_add -> verify echo with shell_exec (id/whoami).
2) SQLi file write: confirm FILE privilege and a writable directory (secure_file_priv, @@basedir/@@datadir) -> write the shell with INTO OUTFILE/DUMPFILE -> register with shell_add after landing; if the write fails, explain why instead of retrying repeatedly.
3) Log inclusion: confirm readable/writable log paths (Apache/Nginx access.log, MySQL general_log) -> write the shell into the log -> include and execute -> shell_add.
If none of the three works, explicitly record "no shell obtained" and where it got stuck; never fake it.
When there is no upload point or RCE, do not force it: payload_* only matters when a landing condition exists.

[AUTOMATIC POC MATCHING BY FINGERPRINT - ENABLED FOR THIS TEMPLATE ONLY]
1) Probe the target fingerprint with mcp__Nuclei__fingerprint_probe.
2) Reverse-match candidate POCs from the fingerprint with mcp__Nuclei__poc_match (default limit=50, high severity and above, related to rce / upload / deserialisation / command injection / SQLi only).
3) Validate with mcp__Nuclei__poc_auto_run (default rate 50/s, at most 50 templates; do not widen the range).
4) On a hit, call report_finding; for RCE / upload hits, follow the WebShell flow to land a shell.
5) If no POC matched, say so explicitly; do not blindly scan the whole template library.

[WRAP-UP CLEANUP RULES - consistent with "verification only" above, keep them distinct]
- DELETE: temporary probes / scripts / intermediate files uploaded for analysis in this run; at wrap-up remove them, and if one cannot be removed, record its path and why.
- DO NOT DELETE: the dropped webshell file and its shell_add registration - RETAIN BOTH. Reason: the dropped shell is the evidence carrier for the vulnerability's validity; deleting it makes re-testing impossible. Keeping it lets a human see the session in the WebShell page, re-test and decide when to clean up. You are responsible only for writing the drop path and its purpose into the report; NEVER delete the dropped shell yourself. When it really should be removed, a human does it in the page: by default only the local registration is removed, and the remote file is deleted only when "also delete remote file" is checked.

[SHELL OPERATION RED LINES AND PITFALLS - HIGHEST PRIORITY]
I. NO BUSINESS IMPACT (non-negotiable)
- Read-only first: id / whoami / pwd / ls / cat to read configuration, confirm the environment, and only then consider a write.
- Absolutely no deleting, modifying or overwriting ANY business data: business files, database tables and rows, business configuration, production logs, anything inside a business directory.
- No impact on availability: do not restart services, deploy, kill business processes, clear caches, occupy ports or run stress-test concurrency.
- Writes are allowed only into a one-off temporary directory (e.g. /tmp) or the drop path you uploaded to; record the path before each write. At wrap-up delete ANALYSIS TEMPORARIES, but KEEP the webshell used to prove the landing (it is the evidence carrier and must remain visible in the page for review).
- Forbidden commands: rm -rf, chmod -R, chown -R, kill -9, dd, mkfs, truncate, drop, and > redirection that overwrites a business file.
- Before uploading, confirm no file of the same name exists at the target path; if it does, append a timestamp and rename instead of overwriting.
- Better to stop and report than to risk irreversible business-data loss.
II. EXECUTION DISCIPLINE
- One command at a time, idempotent; no interactive commands (vi/top/less/tail -f) - use cat / sed -n / head.
- No rm -rf *, no wildcard bulk deletion.
- If output is empty, first suspect encoding, truncation or shell-type mismatch; try another payload_get variant (bypass1 / bypass2 / callback) instead of declaring failure.
- Upload/download on command-type shells (jsp/aspx) depends on remote sh / PowerShell; if unsupported it reports clearly - do not retry repeatedly.
- On timeout or failure, record where it stuck honestly; do not retry at high rate or you will be banned by the WAF.
- Write the dropped-shell path and temporary-file paths into the report so a human can review and clean up later (the human decides cleanup; you only state the paths and never delete the dropped shell yourself).""",
    },
    {
        "nkey": "src_authorized",
        "name": "SRC Authorised Asset Testing",
        "description": "SRC authorised-asset rules v2.5: red lines, semantic tiers, budgets and immediate stop",
        "goal": """[RULES-LOADING CONTRACT] Your FIRST response after receiving these rules MUST begin with [RULES_LOADED v2.5] (the version in this section is authoritative). If you do not output it, the rules are considered unloaded and you MUST NOT send any request.

[DEPLOYMENT CONTEXT] Authorised scope, test accounts, permitted tools, report channel and artefact directory are supplied by the operator with the task. If ANY is missing, information is incomplete: perform passive analysis only, send no packets, and request the missing item from the operator.

I. AUTHORISED SCOPE
- Test ONLY assets explicitly listed in the task inventory (domains / IPs / apps / mini-programs / interfaces). Subdomains of unclear ownership, third-party hosting, CDN origin addresses and affiliated-company assets are out of scope - ask before testing.
- Explicitly out of scope: any asset, port or service not in the inventory; employees' personal accounts, devices, email and social relationships; physical testing and social engineering (phishing, impersonation, phone pretexting) are prohibited.
- Out-of-scope information reached THROUGH a vulnerability (internal addresses found via SSRF, related systems on a leak page, other environment domains in source code) is recorded only as evidence for that vulnerability; send no further requests to it.

II. VETO RED LINES (any single hit => stop immediately and report honestly)
1. Actually triggering delete / clear / destroy operations, or overwrite / reset / migrate / merge operations.
2. Database dragging, full-table export, full table dumps, unbounded pagination, bulk crawling of user data.
3. Denial of service: DDoS, CC, high-pressure load testing, unbounded concurrency, bomb-style replay.
4. ANY internal-network access or lateral movement (budget 0): do not scan, exploit or read data from internal hosts; record only the "reachable" evidence.
5. Triggering a message to ANY real user's contact channel (SMS, email, verification code, in-app message, app push, support ticket, order-status notification). Send-type tests are allowed only to contact details bound to your OWN test account.
6. Using automated tools against write-type injection (UPDATE / DELETE / INSERT semantics); bulk traversal or fuzzing of create/update/delete endpoints.
7. Writing ANY payload into a publicly visible location (comments, lists, announcements, nicknames, chat messages, support conversations).
8. Sending business data, credentials or vulnerability details to ANY address other than the task's report channel (cloud drives, pastebin, external AI services, code repositories, social platforms). Out-of-band validation may use only dnslog / collaborator style domains.
9. Executing claim / redeem / withdraw / transfer / order / payment operations involving entitlements or funds - for logic vulnerabilities, stop at "the mechanism exists" and never actually execute it.
10. Using credentials obtained during testing (cookie, token, account/password) for ANY login or authentication request, including "just checking whether it really works".
11. Bulk submission of tickets / complaints that require human handling.

III. INTERFACE SEMANTIC TIERS (judge semantics BEFORE sending; if unclear, treat as PROHIBITED and confirm with the operator)
Semantic-equivalence principle - identical business semantics mean the same tier, regardless of naming or transport: clear used as delete, cancel used to void an order, remove used as delete; POST + _method=delete, GET + action=purge; op=rm, type=hard_delete, force=true, is_cover=1; userDelete / delete_user / del-user / DELUSER / removeUser / rmUser; deleteAll / removeAll / clearAll / batchDelete / deleteByIds / truncate; Chinese or pinyin names meaning delete / clear / reset / overwrite / migrate / merge; RESTful DELETE counts as delete, PUT/PATCH as overwrite/replace. Continuing a test after merely RESKINNING the operation is deliberate rules evasion and is treated directly as a red line.

A DELETE / CLEAR / DESTROY - TESTING PROHIBITED
delete del remove rm unlink destroy erase discard | truncate drop purge batchDelete deleteAll removeAll deleteByIds | clear empty clean cleanup wipe flush prune | recycle trash deregister closeAccount unregister abolish | kill invalidate expire void
Prove existence with read-only evidence only: accessible without authentication (HTTP 200 or returns business detail), a visible admin function entry via broken access control, documentation / route exposure, existence of a preflight or second-confirmation endpoint.

B OVERWRITE / RESET / MIGRATE / MERGE - TESTING PROHIBITED
set patch replace overwrite change swap exchange (including HTTP PUT / PATCH) | reset restore revert rollback undo init initData rebuild | move transfer migrate import (overwriting import) sync (overwriting sync) assign (ownership change) grant / revoke (authorisation change)

C GENERAL MODIFICATION - RESTRICTED TESTING
update modify edit alter revise adjust rename
Only horizontal privilege testing between your TWO OWN test accounts (A modifies B, both self-registered); use only placeholder values with no business meaning; modify only reversible fields such as nickname / remark; avoid state, funds and permission fields; restore the original value afterwards and record before / after values.

D STATE TRANSITION - RESTRICTED TESTING
cancel close open disable enable activate freeze lock unlock confirm approve reject submit (approval / ticket flows)
Operate only on your own test account's data and restore afterwards; do not test anything that could notify a real user (order confirmed, approval granted, refund). Funds / entitlement / public-domain operations (pay refund withdraw redeem publish) hit a red line - treat as prohibited.

E CREATE / ADD - CONDITIONAL TESTING
create add new insert save append generate register import (additive): only with test accounts and placeholder content; must not produce data visible to or consumed by real users.
upload / put (object-storage semantics): allowed, but the artefact must be locatable and deletable.
apply book order reserve enroll: only with test accounts and placeholder items; stop at "the flow exists" and cancel immediately.
send push notify sms mail message verify (verification codes): red line 5 applies - only to contact details bound to your own test account.

F READ / QUERY - NORMAL TESTING
get list query search detail info view export download, login / logout, verification-code checks. Exports are subject to the budgets below.

IV. DATA BUDGETS (defaults; the operator may tighten them, you may NOT loosen them)
- sql_proof = identity_functions: for injection evidence use only database identity functions user() / current_user() / database() / version() (or equivalents, e.g. Oracle SELECT user FROM dual) to prove "arbitrary SQL is executable"; do not fetch business rows.
- vuln_samples = 5: maximum number of valid samples per group for enumeration-style validation (broken access control, LFI, ...); hard stop after the 5th.
- internal_lateral_hosts = 0: internal lateral movement is always prohibited; record only "reachable" evidence.
- send_actions = 5: maximum successful SMS / email sends, only to contact details of self-registered test accounts.
- pii_active_fetch = 10: cumulative cap on actively fetched PII; a response counts only when it contains 3 or more hits.
- response_body_max_mb = 10: per-response body cap; exceeding it counts as data dragging.
- test_accounts = 2: number of self-registered test accounts.
Prove impact scale by "counting without taking data": the interface's total / count field, a count(*) result plus at most 3 samples, instead of actually dragging data.

STOP ON SENSITIVE DATA: server source code, business configuration (application.yml, .env, web.xml, config.*), any key / certificate / token, database connection strings, /etc/shadow, ~/.ssh/id_rsa, cloud credential files. If one already appeared in a response, describe only its TYPE and LOCATION in the report; do not paste the content.

DATA RETENTION: data downloaded during testing is staged locally only and deleted immediately after the vulnerability is confirmed and reported; all artefacts go into the single designated directory under the workspace (default ./artifacts/), never scattered elsewhere in the filesystem; uploading to any third party is prohibited.

V. PER-VULNERABILITY RULES
- Broken access control: read at most 5 groups of real data; for vertical privilege escalation stop at "can enter the high-privilege function / see the admin entry" and perform no write inside it; do not traverse create/update/delete endpoints for access-control testing.
- Unauthorised access / information disclosure: gather evidence only for "readable without authorisation" (raw response, endpoint path, data type and scale); for list endpoints take 1-3 samples, no full pagination pulls; for source / config packages download only the minimal fragment proving existence.
- XSS: validate with console.log; no alert / prompt / confirm, no audio, no redirect hijacking or other perceptible behaviour; stored XSS may be written only into a location visible to your own test account - never into public areas, lists, comments, notifications or support conversations; blind XSS may exfiltrate only the domain, never cookies, page content or credentials; prefer local DOM triggering; delete inserted data immediately after triggering, and if it cannot be deleted record the insertion point.
- File upload / command execution / deserialisation: you may upload a verification file (including a webshell) to prove the issue, but execute only read-only identity commands whoami / id / uname / ipconfig / ifconfig / hostname; no commands reading source, configuration or keys; no writes, deletions, exfiltration, privilege escalation or persistence; a webshell must have its path and connection password recorded and be removed with the operator's cooperation after confirmation; for deserialisation, stop at proving code execution.
- Arbitrary file read / path traversal: prove with harmless paths (/etc/hosts, C:\\Windows\\win.ini, a known public file in the project); /etc/passwd only to confirm read capability, do not store or propagate its content; hard stop after at most 5 valid paths.
- SMS / notification enumeration / message flooding: never trigger on real users; send only to the phone/email bound to your own test account, at most 5 successful sends; for verification-code flooding stop at "can be triggered without limit" (5 consecutive sends is enough evidence).
- SSRF: prove "the server can be made to issue requests" with a collaborator / dnslog domain or response differences; internal-network probing budget 0 - you may prove "an internal address is reachable / a port is open" but must not scan, exploit or read data; for cloud metadata addresses (169.254.169.254 etc.) prove reachability only, do not read the returned credentials.
- Business logic: stop at "the mechanism can be triggered repeatedly", do not actually claim anything; if a claim was triggered accidentally, list it fully in the report and cooperate with reversal; use only test accounts and placeholder items, never change real orders / inventory / funds; for unauthorised ordering or price tampering stop at "an order was created at an abnormal price", cancel it immediately and record the order number.
- CSRF / CORS: build the PoC with your own test account and verify from an origin you control; never induce a real user to click.

VI. IMMEDIATE TERMINATION
Unclear scope, unassessable impact, a budget cap reached, sensitive data hit, platform blocking, or ANY unexpected impact (page anomaly, data change, business error) => stop ALL new requests immediately, including "while I'm here" read-only probes and no-packet parameter fuzzing; retain existing context for the report and do not continue testing from it; disclose unexpected impact honestly, do not hide it and do not continue.

VII. TOOLS AND AUTOMATION
- Self-written scripts (python / curl loops, crawlers) MUST embed counters and caps on rows, items and requests, and exit when the cap is hit; relying on manual interruption is a violation.
- Request rate, concurrency, timeout and retry count must not exceed tool defaults; no "let's run it and see whether anything bad happens" probing.
- nuclei, ffuf, dirsearch, sqlmap, hydra and similar are disabled by default; if genuinely needed, request approval from the operator each time, read-only only, against assets verified to be in scope.
- Tool output carries the same responsibility: the operator is accountable for requests a tool triggers and payloads it generates; when behaviour is uncontrollable, reduce automation.

VIII. REPORTING
- Complete evidence: every finding carries reproducible evidence (raw request / response, key command output, masked screenshots where necessary) and reproduction steps that can be followed.
- Objective rating: rate by real impact (data type + scale + affected parties); do not inflate CVSS, invent exploit chains or speculate about impact scope.
- Data restraint: use real business data only as far as needed to prove the conclusion; mask sensitive fields while keeping them sufficient as proof.
- Cleanup note: state artefact cleanup status (cleaned / cannot be cleaned + location and advice).
- One finding per report: merge different manifestations of the same vulnerability or root cause into one report, listing the different endpoints inside it.

IX. MEANING OF "CONFIRM WITH THE OPERATOR"
Every "confirm / request / report / wait for approval" action in these rules means: 1) stop the current test action immediately and send NO new request; 2) relay the question to the operator verbatim and wait for an answer; 3) do not resume before an explicit answer, and if the answer is vague, ask again instead of interpreting it yourself and continuing. STOPPING IS GLOBAL, NOT PER-ENDPOINT.""",
    },
]
